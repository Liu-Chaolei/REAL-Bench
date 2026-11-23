"""Private authoring records built around the existing native task models."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from tau2.config import (
    DEFAULT_GAIA2_SERIES_ATTEMPTS,
    DEFAULT_GAIA2_SERIES_CONCURRENCY,
    DEFAULT_GAIA2_SERIES_CONTEXT_CHARACTERS,
    DEFAULT_GAIA2_SERIES_REASONING_EFFORT,
    DEFAULT_SEED,
)
from tau2.data_model.scenario import ScenarioAction
from tau2.data_model.tasks import EvaluationCriteria


class StrictModel(BaseModel):
    """Reject accidental fields rather than silently changing task semantics."""

    model_config = ConfigDict(extra="forbid")


def validate_literal_reference(value: str, field: str) -> None:
    """Reject runtime templates in fields that are read literally."""
    if "{{" in value or "}}" in value:
        raise ValueError(
            f"{field} must be literal; {{{{event_id}}}} expands only in "
            "event.arguments or evidence.public_arguments values. "
            "Use source=result, a literal event_id and pointer='' for a scalar "
            "result; use a public getter result for object fields."
        )


def validate_json_pointer(value: str) -> None:
    """Require a static RFC 6901 pointer, including valid escaping."""
    validate_literal_reference(value, "JSON pointer")
    if value and not value.startswith("/"):
        raise ValueError(
            "Use an RFC 6901 JSON pointer, starting with /; '' selects the whole result"
        )
    if re.search(r"~(?![01])", value):
        raise ValueError("Escape JSON pointer keys with ~0 for ~ and ~1 for /")


class ResultKeyRef(StrictModel):
    """A completed event result supplying a dictionary key, without templates."""

    event_id: str
    pointer: str = ""

    @model_validator(mode="after")
    def literal_key_reference(self):
        validate_literal_reference(self.event_id, "Key result event_id")
        validate_json_pointer(self.pointer)
        return self


class ValueRef(StrictModel):
    """An RFC 6901 pointer into a snapshot, stage state, or operation result.

    Stage zero is the completed original task, stages one/two are continuations.
    Snapshot pointers address an app-name keyed mapping of initial app states.
    State pointers address the same mapping at the specified checkpoint. A
    reference speech result exposes /content from its actual delivery journal.
    """

    source: Literal["snapshot", "state", "result"]
    stage: int = Field(default=0, ge=0, le=2)
    event_id: str | None = Field(
        default=None,
        description="Literal producing event ID for source=result, never {{...}}.",
    )
    pointer: str = Field(
        default="",
        description="Static RFC 6901 path in the actual value, never {{...}}. Empty string selects the entire result, including scalar IDs.",
    )
    key_from_result: ResultKeyRef | None = None
    suffix_pointer: str = ""

    @model_serializer(mode="wrap")
    def preserve_legacy_bytes(self, handler):
        data = handler(self)
        if self.key_from_result is None and not self.suffix_pointer:
            data.pop("key_from_result", None)
            data.pop("suffix_pointer", None)
        return data

    @model_validator(mode="after")
    def valid_pointer(self):
        validate_json_pointer(self.pointer)
        if self.source == "result" and not self.event_id:
            raise ValueError("Result references require an event_id")
        if self.event_id:
            validate_literal_reference(self.event_id, "Result event_id")
        validate_json_pointer(self.suffix_pointer)
        if self.key_from_result:
            self.key_from_result.literal_key_reference()
            if self.source != "state":
                raise ValueError("key_from_result requires source=state")
        elif self.suffix_pointer:
            raise ValueError("suffix_pointer requires key_from_result")
        return self


class Evidence(StrictModel):
    """An independently checked material fact supporting the new request."""

    reference: ValueRef
    expected: Any
    explanation: str = Field(min_length=1)
    public_tool: str = Field(min_length=1)
    public_arguments: dict[str, Any] = Field(default_factory=dict)
    result_pointer: str = Field(
        default="",
        description="Static RFC 6901 path into the public read's actual return. Use '' for a scalar, /0/field for a list, /field for an object. No {{...}}.",
    )

    @model_validator(mode="after")
    def concrete_result_pointer(self):
        validate_json_pointer(self.result_pointer)
        return self


class OutcomeCheck(StrictModel):
    """A deterministic assertion; no generated Python or eval is accepted."""

    reference: ValueRef
    operation: Literal[
        "equals", "contains", "count", "set_equals", "sum", "min", "max"
    ] = "equals"
    expected: Any = None
    compare_to: ValueRef | None = None
    explanation: str = Field(min_length=1)


class StageDraft(StrictModel):
    """One new request, its native reference events, and its checked oracle."""

    request: str = Field(min_length=12)
    relation: str = Field(min_length=12)
    preconditions: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(min_length=1)
    events: list[ScenarioAction] = Field(min_length=1)
    criteria: EvaluationCriteria
    checks: list[OutcomeCheck] = Field(min_length=1)

    @model_validator(mode="after")
    def literal_event_identifiers(self):
        for event in self.events:
            validate_literal_reference(event.id, "event ID")
            for dependency in event.dependencies:
                validate_literal_reference(dependency, "dependency ID")
        return self


class SeriesDraft(StrictModel):
    """A whole candidate; original task bytes remain in the sealed source."""

    base_task_id: str
    base_task_sha256: str
    stages: list[StageDraft] = Field(default_factory=list, max_length=2)


class RepairResult(StrictModel):
    """A replacement candidate with an auditable explanation of its changes."""

    stage: StageDraft
    change_kind: Literal["fix", "replace"] = "fix"
    reason: str = Field(min_length=1)
    replacement_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def justify_replacement(self):
        if self.change_kind == "replace" and not self.replacement_evidence:
            raise ValueError("Replacing a request requires material/tool evidence")
        return self


class RepairChange(StrictModel):
    """Replace an existing value within the current stage only."""

    op: Literal["replace"] = "replace"
    path: str
    value: Any

    @model_validator(mode="after")
    def allowed_path(self):
        validate_json_pointer(self.path)
        root = self.path.split("/")[1] if self.path.startswith("/") else ""
        if root not in {
            "evidence",
            "events",
            "checks",
            "criteria",
            "relation",
            "preconditions",
        }:
            raise ValueError(
                "Repair paths must target the current stage's events, evidence, "
                "checks, criteria, relation or preconditions; request is locked"
            )
        return self


class RepairPatch(StrictModel):
    """Content-bound edits; the host constructs and validates RepairResult."""

    base_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[RepairChange] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1)


class ModelProfile(StrictModel):
    """Explicit model settings and environment-variable credential references."""

    model: str | None = None
    model_env: str | None = None
    api_role: str | None = None
    api_key_env: str | None = None
    api_base_env: str | None = None
    provider_prefix: str = "openai/"
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = (
        DEFAULT_GAIA2_SERIES_REASONING_EFFORT
    )
    model_args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def explicit_model(self):
        if sum(bool(x) for x in (self.model, self.model_env, self.api_role)) != 1:
            raise ValueError("Choose exactly one of model, model_env or api_role")
        if {"api_key", "api_base", "base_url"} & self.model_args.keys():
            raise ValueError("Use credential environment references, not model_args")
        return self


class AuthoringConfig(StrictModel):
    """Pinned settings shared by generation, review, and resume checks."""

    author: ModelProfile
    reviewer: ModelProfile
    api_config_dir: str | None = None

    @model_validator(mode="before")
    @classmethod
    def shared_api_roles(cls, value):
        if isinstance(value, dict) and value.get("api_config_dir"):
            value = dict(value)
            for role in ("author", "reviewer"):
                profile = value.get(role)
                if profile and not profile.get("api_role"):
                    raise ValueError(
                        "api_config_dir conflicts with explicit authoring connection"
                    )
                value[role] = {"api_role": role.upper(), **(profile or {})}
        return value

    config_env_file: str | None = None
    concurrency: int = Field(default=DEFAULT_GAIA2_SERIES_CONCURRENCY, ge=1)
    max_attempts: int = Field(default=DEFAULT_GAIA2_SERIES_ATTEMPTS, ge=1)
    seed: int = DEFAULT_SEED
    context_characters: int = Field(
        default=DEFAULT_GAIA2_SERIES_CONTEXT_CHARACTERS, ge=10000
    )


class RequestPolicy(StrictModel):
    """Operational overrides; changing them does not replace accepted task content."""

    timeout_seconds: float | None = Field(default=None, gt=0)
    attempts: int = Field(default=1, ge=1, le=3)
    retry_delay_seconds: float = Field(default=1, ge=0, le=10)
    stop_batch_on_error: bool = False
    api_key_pool: list[str] = Field(default_factory=list)
    per_key_concurrency: int = Field(default=6, ge=1)
    per_key_concurrency_overrides: dict[str, int] = Field(default_factory=dict)
    adaptive_concurrency: bool = True
    deadline_at: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_key_pool(self):
        if len(self.api_key_pool) != len(set(self.api_key_pool)) or any(
            not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in self.api_key_pool
        ):
            raise ValueError(
                "API key pool must contain unique environment variable names"
            )
        if set(self.per_key_concurrency_overrides) - set(self.api_key_pool):
            raise ValueError(
                "Per-key concurrency overrides must name a configured pool key"
            )
        if any(not 1 <= n <= 128 for n in self.per_key_concurrency_overrides.values()):
            raise ValueError("Per-key concurrency overrides must be between 1 and 128")
        return self


class ReviewDecision(StrictModel):
    """All quality dimensions must pass before an automated acceptance."""

    related: bool
    grounded: bool
    uses_previous_result: bool
    substantive: bool
    correct_solution: bool
    unambiguous: bool
    no_answer_leak: bool
    no_pointless_reversal: bool
    reasons: list[str] = Field(default_factory=list)
    affected_stages: list[Literal[1, 2]] = Field(default_factory=list)
    material_warnings: list[str] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return all(
            getattr(self, field)
            for field in type(self).model_fields
            if field not in {"reasons", "affected_stages", "material_warnings"}
        )


class HumanDecision(StrictModel):
    """A human signature applies only to one exact candidate content hash."""

    base_task_id: str
    content_sha256: str
    reviewer: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"]
    notes: str = ""

"""Private, versioned live scenarios; never sent to the evaluated agent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from tau2.config import (
    DEFAULT_PERSISTENT_TASK_MAX_WAIT_SECONDS,
    DEFAULT_PERSISTENT_TASK_RELEASE_DELAY_SECONDS,
)
from tau2.persistent_tasks.core.models import CanonicalState
from tau2.persistent_tasks.core.overlays import BusinessOverlay
from tau2.persistent_tasks.core.projection import BackendTaskProjector


class BindingConfig(BaseModel):
    """A source-stage role selected from real native creation evidence."""

    model_config = ConfigDict(extra="forbid")
    role: str = "primary"
    source_task_id: str
    source_stage: int = Field(ge=0)
    activation_event_ids: list[str] = Field(default_factory=list)
    objective_contains: str | None = None
    objective_prefix: str | None = None

    @model_serializer(mode="wrap")
    def serialize_prefix(self, handler):
        """Preserve older source-binding hashes without a prefix rule."""
        data = handler(self)
        if self.objective_prefix is None:
            data.pop("objective_prefix", None)
        return data


class RequestConfig(BaseModel):
    """Private release conditions and expected native operation for one turn."""

    model_config = ConfigDict(extra="forbid")
    request_id: str
    text: str = Field(min_length=1)
    role: str = "primary"
    state: CanonicalState
    operation: Literal["query", "result", "modify", "cancel"] = "query"
    delay_seconds: float = Field(
        default=DEFAULT_PERSISTENT_TASK_RELEASE_DELAY_SECONDS, ge=0, allow_inf_nan=False
    )
    max_wait_seconds: float = Field(
        default=DEFAULT_PERSISTENT_TASK_MAX_WAIT_SECONDS, gt=0, allow_inf_nan=False
    )
    reply_expectation: str = Field(min_length=1)
    expected_arguments: dict = Field(default_factory=dict)
    allow_status_list_lookup: bool = False
    request_operation_policy: Literal[
        "post_delivery_only", "allow_fresh_prefetch", "prefetch_diagnostic_only"
    ] = "post_delivery_only"
    prefetch_max_age_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)

    @model_serializer(mode="wrap")
    def serialize_lookup_policy(self, handler):
        """Keep existing frozen profiles unchanged when no helper is allowed."""
        data = handler(self)
        if not self.allow_status_list_lookup:
            data.pop("allow_status_list_lookup", None)
        if self.request_operation_policy == "post_delivery_only":
            data.pop("request_operation_policy", None)
        if self.prefetch_max_age_seconds == 120:
            data.pop("prefetch_max_age_seconds", None)
        return data

    @model_validator(mode="after")
    def supported_operation_state(self):
        """Reject impossible result queries and terminal task restarts."""
        if self.operation == "result" and self.state != CanonicalState.COMPLETED:
            raise ValueError("result requests require a completed task")
        if (
            self.operation in {"modify", "cancel"}
            and self.state in BackendTaskProjector.TERMINAL
        ):
            raise ValueError("v1 mutations cannot reopen terminal tasks")
        return self


class PersistentTaskConfig(BaseModel):
    """Resolved sidecar with a single native source stage per call."""

    model_config = ConfigDict(extra="forbid")
    version: Literal["persistent-task-v1", "qwen-persistent-v1"] = "persistent-task-v1"
    scenario_id: str
    status: Literal["draft", "verified"] = "draft"
    bindings: list[BindingConfig] = Field(min_length=1)
    requests: list[RequestConfig] = Field(min_length=1)
    combine_reward: bool = False
    overlay: BusinessOverlay | None = None
    initial_request_prefix: str | None = Field(
        default=None, min_length=1, max_length=2000
    )
    reply_evidence_mode: Literal["generated_text", "played_audio", "both"] = (
        "generated_text"
    )

    @model_serializer(mode="wrap")
    def serialize_initial_prefix(self, handler):
        """Only record a public setup revision when the scenario declares one."""
        data = handler(self)
        if self.initial_request_prefix is None:
            data.pop("initial_request_prefix", None)
        if self.reply_evidence_mode == "generated_text":
            data.pop("reply_evidence_mode", None)
        return data

    @field_validator("overlay", mode="before")
    @classmethod
    def empty_overlay(cls, value):
        """Accept historical draft query profiles with an empty overlay."""
        return None if value == {} else value

    @model_validator(mode="after")
    def consistent(self):
        """Validate identities and mutation goals before connecting to models."""
        roles = [item.role for item in self.bindings]
        ids = [item.request_id for item in self.requests]
        if len(roles) != len(set(roles)) or len(ids) != len(set(ids)):
            raise ValueError("binding roles and request IDs must be unique")
        if len({(b.source_task_id, b.source_stage) for b in self.bindings}) != 1:
            raise ValueError("v1 scenarios must remain within one source task/stage")
        if self.initial_request_prefix and self.bindings[0].source_stage != 0:
            raise ValueError(
                "Public setup prefixes only apply to the original first request"
            )
        if any(r.role not in roles for r in self.requests):
            raise ValueError("unknown request task role")
        if any(
            r.state in {CanonicalState.UNKNOWN, CanonicalState.NOT_STARTED}
            for r in self.requests
        ):
            raise ValueError("unknown or not_started evidence cannot trigger a request")
        mutation = any(r.operation in {"modify", "cancel"} for r in self.requests)
        if mutation and self.overlay is None and self.status == "draft":
            # Draft selection may reserve a mutation slot before its authored
            # business overlay exists. Preflight will mark it needs_revision;
            # verified/live configs still require an explicit overlay.
            return self
        if mutation != (self.overlay is not None):
            raise ValueError(
                "mutation requests require an explicit business-goal overlay"
            )
        return self

    @classmethod
    def load(cls, path):
        """Load an explicit sidecar rather than implicitly enabling old fixtures."""
        return cls.model_validate_json(Path(path).read_text())

    def digest(self):
        """Return the complete resolved sidecar's stable configuration hash."""
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()

    def validate_task(self, task):
        """Check immutable source references before creating a live provider."""
        if task.scenario is None:
            raise ValueError("persistent tasks require a Gaia2 scenario")
        if self.combine_reward:
            from tau2.data_model.tasks import RewardType

            basis = (
                self.overlay.criteria.reward_basis
                if self.overlay
                else (
                    task.evaluation_criteria.reward_basis
                    if task.evaluation_criteria
                    else []
                )
            )
            if RewardType.PERSISTENT_TASK not in basis:
                raise ValueError(
                    "Combined reward requires an explicit PERSISTENT_TASK task reward basis"
                )
        if self.overlay:
            self.overlay.derive(task)
            if self.overlay.stage != self.bindings[0].source_stage:
                raise ValueError(
                    "overlay and persistent bindings must use the same stage"
                )
        for binding in self.bindings:
            source_id = (
                task.scenario.source_id
                if task.scenario.async_interaction is not None
                else task.id
            )
            if binding.source_task_id != source_id or binding.source_stage >= len(
                task.scenario.stages
            ):
                raise ValueError(
                    "persistent config does not match the selected task/stage"
                )
            stage = task.scenario.stages[binding.source_stage]
            if not set(binding.activation_event_ids) <= set(stage.event_ids):
                raise ValueError("activation event outside selected source stage")

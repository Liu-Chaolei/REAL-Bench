"""Business-only model submissions assembled into the existing stage format."""

from typing import Any

from pydantic import Field, model_validator

from tau2.data_model.scenario import ScenarioAction
from tau2.data_model.tasks import EvaluationCriteria, RewardType
from tau2.task_authoring.gaia2_series.compiler import ANSWER_TOOL
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.schema import (
    Evidence,
    OutcomeCheck,
    StageDraft,
    StrictModel,
)
from tau2.task_authoring.gaia2_series.storage import digest


class SolutionOperation(StrictModel):
    """Only the model-selected operation, without host-owned event metadata."""

    id: str = Field(pattern=r"^series_s[12]_[A-Za-z0-9_]+$")
    tool: str
    arguments: dict[str, Any]
    dependencies: list[str] = Field(default_factory=list)


class StageSolution(StrictModel):
    """The model cannot submit requests, DB rewards, action rewards or channels."""

    events: list[SolutionOperation] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    checks: list[OutcomeCheck] = Field(min_length=1)
    communicate_info: list[str] = Field(default_factory=list)
    nl_assertions: list[str] = Field(min_length=1)


class SolutionRevision(StrictModel):
    """Replace complete business blocks; there is no arbitrary patch path."""

    base_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    events: list[SolutionOperation] | None = Field(default=None, min_length=1)
    evidence: list[Evidence] | None = Field(default=None, min_length=1)
    checks: list[OutcomeCheck] | None = Field(default=None, min_length=1)
    communicate_info: list[str] | None = None
    nl_assertions: list[str] | None = Field(default=None, min_length=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def at_least_one_block(self):
        if all(
            getattr(self, field) is None
            for field in (
                "events",
                "evidence",
                "checks",
                "communicate_info",
                "nl_assertions",
            )
        ):
            raise ValueError("Supply at least one solution or answer/assertion block")
        return self


def assemble_solution(solution: StageSolution, request: str, stage: int) -> StageDraft:
    """Inject locked text, framework criteria and deterministic event metadata."""
    events = []
    for index, operation in enumerate(solution.events):
        if (
            not operation.id.startswith(f"series_s{stage}_")
            or operation.id == f"series_s{stage}_user"
        ):
            raise CandidateValidationError(
                f"Use a unique series_s{stage}_ prefixed event ID; the user event is host-owned",
                phase="compile",
                stage=stage,
                code="invalid_event_id",
                path=f"/events/{index}/id",
                actual=operation.id,
                expected=f"series_s{stage}_<operation_name>",
            )
        payload = operation.model_dump(mode="json")
        if operation.tool == ANSWER_TOOL:
            arguments = payload["arguments"]
            if set(arguments) == {"message"}:
                arguments["content"] = arguments.pop("message")
            if (
                set(arguments) != {"content"}
                or not isinstance(arguments["content"], str)
                or not arguments["content"].strip()
            ):
                raise CandidateValidationError(
                    "Reference replies require arguments={'content': '<reply text>'}",
                    phase="submission",
                    stage=stage,
                    code="invalid_reference_answer_arguments",
                    path=f"/events/{index}/arguments",
                    actual={"keys": list(arguments)},
                    expected={"content": "non-empty reply text"},
                )
        events.append(
            ScenarioAction(
                **payload,
                kind="reference",
                channel="agent" if operation.tool == ANSWER_TOOL else "tool",
            )
        )
    basis = [RewardType.ENV_ASSERTION, RewardType.NL_ASSERTION]
    if solution.communicate_info:
        basis.append(RewardType.COMMUNICATE)
    return StageDraft(
        request=request,
        relation="Technical reference for the locked business request.",
        events=events,
        evidence=solution.evidence,
        checks=solution.checks,
        criteria=EvaluationCriteria(
            actions=[],
            env_assertions=[],
            reward_basis=basis,
            communicate_info=solution.communicate_info or None,
            nl_assertions=solution.nl_assertions,
        ),
    )


def revise_solution(candidate: dict, revision: SolutionRevision) -> StageSolution:
    """Validate a content-bound block revision without changing the input object."""
    if digest(candidate) != revision.base_candidate_sha256:
        raise CandidateValidationError(
            "Revision targets a stale candidate hash",
            phase="submission",
            code="stale_candidate",
            path="/base_candidate_sha256",
            actual=revision.base_candidate_sha256,
            expected=digest(candidate),
        )
    updated = dict(candidate)
    for field in ("events", "evidence", "checks", "communicate_info", "nl_assertions"):
        block = getattr(revision, field)
        if block is not None:
            updated[field] = [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in block
            ]
    return StageSolution.model_validate(updated)


def solution_from_stage(stage: StageDraft) -> StageSolution:
    """Convert a verified prefix for diagnostics/tests; never import bad candidates."""
    return StageSolution(
        events=[
            SolutionOperation(
                **event.model_dump(include={"id", "tool", "arguments", "dependencies"})
            )
            for event in stage.events
        ],
        evidence=stage.evidence,
        checks=stage.checks,
        communicate_info=stage.criteria.communicate_info or [],
        nl_assertions=stage.criteria.nl_assertions or [],
    )

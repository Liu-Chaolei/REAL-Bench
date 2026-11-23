"""Small data contract for runtime Modify/Cancel/AddTask variants."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.data_model.scenario import ScenarioAction

VariantType = Literal["addtask", "modify", "cancel"]


class InteractionVariant(BaseModel):
    """A source task plus an explicit runtime request change.

    The source task remains immutable. The runtime consumes this record to add
    a user request and bind evidence; it does not rewrite the source reference
    graph or invent a new operation path.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    version: Literal["gaia2-interaction-variant-v1"] = "gaia2-interaction-variant-v1"
    source_task_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_type: VariantType
    stage: int = Field(ge=0)
    initial_request: str = Field(min_length=1)
    followup_request: str = Field(min_length=1)
    temporary_request: str | None = None
    primary_hint: str = Field(min_length=1)
    target_hint: str = Field(min_length=1)
    target_role: str = "primary"
    preserve_source_goal: bool = True
    scoring_mode: Literal["management_and_existing_evidence", "addtask_result"] = (
        "management_and_existing_evidence"
    )
    metadata: dict = Field(default_factory=dict)
    reference_events: list[ScenarioAction] = Field(default_factory=list)
    reference_answer: str | None = None

    @model_validator(mode="after")
    def valid_content(self):
        """Require authoring content before a variant enters the task loader."""
        if self.variant_type == "cancel" and not self.temporary_request:
            raise ValueError("Cancel requires a separate temporary request B")
        if self.variant_type == "addtask" and (
            not self.reference_events or not self.reference_answer
        ):
            raise ValueError("AddTask requires additional reference events and answer")
        return self

    def request_id(self, phase="followup"):
        """Stable provenance ID, never inserted into spoken user text."""
        return f"{self.id}:{phase}"


def variant_for(task):
    """Read the new variant contract without interpreting legacy async profiles."""
    raw = getattr(getattr(task, "scenario", None), "async_interaction", None)
    if raw and raw.get("version") == "gaia2-interaction-variant-v1":
        return InteractionVariant.model_validate(raw)
    return None


def metric_enabled(task, measure):
    """Choose applicable metrics before any evaluator or judge is called."""
    variant = variant_for(task)
    return not (
        variant
        and variant.variant_type in {"modify", "cancel"}
        and measure in {"environment", "execution", "state_interaction"}
    )


def apply_variant(source, variant):
    """Build a runtime view of a pinned source; references remain private."""
    from tau2.diagnostics.intent.io import digest
    from tau2.orchestrator.scenario_stages import stage_requests

    if (
        source.id != variant.source_task_id
        or digest(source.model_dump(mode="json")) != variant.source_sha256
    ):
        raise ValueError(f"Interaction variant source changed: {variant.id}")
    task = source.model_copy(deep=True)
    task.id = variant.id
    spec = task.scenario
    if variant.stage >= len(spec.stages):
        raise ValueError("Interaction variant stage is outside its source")
    initial = stage_requests(spec)[variant.stage]
    initial.arguments["content"] = variant.initial_request
    spec.stages[variant.stage].request = variant.initial_request
    if variant.stage == 0:
        task.user_scenario.instructions.reason_for_call = variant.initial_request
    spec.async_interaction = variant.model_dump(mode="json")
    spec.category = variant.variant_type
    task.description.purpose = (
        f"Gaia2 {variant.variant_type} interaction variant; source={source.id}"
    )
    if variant.variant_type == "addtask":
        # A and B share no artificial reference ordering. The private user event
        # gives B's references a reproducible anchor in offline ENV replay.
        spec.events.append(
            ScenarioAction(
                id=variant.request_id(),
                kind="user",
                tool=initial.tool,
                arguments={"content": variant.followup_request},
                dependencies=[initial.id],
            )
        )
        spec.events.extend(
            event.model_copy(deep=True) for event in variant.reference_events
        )
        spec.stages[variant.stage].event_ids.extend(
            [variant.request_id(), *(event.id for event in variant.reference_events)]
        )
    return task

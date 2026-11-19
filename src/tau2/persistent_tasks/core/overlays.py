"""Persistent configuration schema for explicit Gaia2 overlays."""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tau2.data_model.scenario import ScenarioAction
from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task
from tau2.task_authoring.gaia2_series.schema import OutcomeCheck


class BusinessOverlay(BaseModel):
    """A private authored revision; no inferred removal of original goals."""

    model_config = ConfigDict(extra="forbid")
    version: Literal["persistent-overlay-v1"] = "persistent-overlay-v1"
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stage: int = Field(ge=0)
    superseded_event_ids: list[str] = Field(min_length=1)
    replacement_events: list[ScenarioAction] = Field(default_factory=list)
    criteria: EvaluationCriteria
    checks: list[OutcomeCheck] = Field(min_length=1)

    def derive(
        self, source: Task, *, completed_before_cancel: set[str] | None = None
    ) -> Task:
        """Keep unaffected events and stages byte-equivalent to the sealed task."""
        spec = source.scenario
        if spec is None or spec.source_sha256 != self.source_sha256:
            raise ValueError("overlay source revision does not match the task")
        if self.stage != len(spec.stages) - 1:
            raise ValueError("v1 mutation overlays require the final source stage")
        stage = spec.stages[self.stage]
        superseded = set(self.superseded_event_ids)
        event_map = {event.id: event for event in spec.events}
        if len(superseded) != len(self.superseded_event_ids) or not superseded <= set(
            stage.event_ids
        ):
            raise ValueError(
                "superseded events must be unique members of the selected stage"
            )
        if any(event_map[key].kind != "reference" for key in superseded):
            raise ValueError("overlays can only supersede reference events")
        replacements = {event.id: event for event in self.replacement_events}
        if (
            len(replacements) != len(self.replacement_events)
            or not set(replacements) <= superseded
        ):
            raise ValueError("replacement IDs must be unique and explicitly superseded")
        if any(event.kind != "reference" for event in replacements.values()):
            raise ValueError("replacement events cannot inject user/environment inputs")
        retained = superseded & (completed_before_cancel or set())
        superseded -= retained
        replacements = {
            key: event for key, event in replacements.items() if key not in retained
        }
        derived = source.model_copy(deep=True)
        derived.scenario.events = [
            replacements[event.id].model_copy(deep=True)
            if event.id in replacements
            else event
            for event in derived.scenario.events
            if event.id not in superseded or event.id in replacements
        ]
        remaining = {event.id for event in derived.scenario.events}
        if any(
            not set(event.dependencies) <= remaining
            for event in derived.scenario.events
        ):
            raise ValueError("overlay leaves an unresolved reference dependency")
        changed = derived.scenario.stages[self.stage]
        changed.event_ids = [key for key in changed.event_ids if key in remaining]
        changed.criteria = self.criteria.model_dump(mode="json")
        changed.checks = [check.model_dump(mode="json") for check in self.checks]
        # Source stage zero owns the shared business-state assertion. Keep it;
        # its reference execution will now use the explicitly revised graph.
        original_assertions = [
            assertion
            for part in spec.stages
            for assertion in part.criteria.get("env_assertions", [])
            if part.stage != self.stage
            or assertion.get("func_name") == "assert_scenario_goal"
        ]
        original_assertions.extend(
            self.criteria.model_dump(mode="json")["env_assertions"] or []
        )
        if not original_assertions:
            raise ValueError(
                "overlay source requires a retained business-state assertion"
            )
        criteria = deepcopy(source.evaluation_criteria.model_dump(mode="json"))
        criteria["communicate_info"] = [
            fact
            for part in derived.scenario.stages
            for fact in part.criteria.get("communicate_info", [])
        ]
        criteria["nl_assertions"] = [
            f"Apply ONLY to stage {part.stage}: {fact}"
            for part in derived.scenario.stages
            for fact in part.criteria.get("nl_assertions", [])
        ]
        criteria["env_assertions"] = original_assertions
        criteria["reward_basis"] = sorted(
            (
                set(criteria["reward_basis"])
                | {basis.value for basis in self.criteria.reward_basis}
            )
            - {RewardType.PERSISTENT_TASK.value}
        )
        derived.evaluation_criteria = EvaluationCriteria.model_validate(criteria)
        return derived

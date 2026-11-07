"""Compile authoring stages into existing Task/ScenarioAction structures."""

import re

from tau2.data_model.scenario import ScenarioAction
from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.schema import (
    SeriesDraft,
    validate_literal_reference,
)
from tau2.task_authoring.gaia2_series.storage import digest

USER_TOOL = "AgentUserInterface__send_message_to_agent"
ANSWER_TOOL = "AgentUserInterface__send_message_to_user"
REFERENCE = re.compile(r"\{\{([^{}]+)\}\}")


def leaves(events: list[ScenarioAction]) -> list[str]:
    """Return all terminal events, including original environment/clarification events."""
    parents = {parent for event in events for parent in event.dependencies}
    return [event.id for event in events if event.id not in parents]


def validate_graph(events: list[ScenarioAction]) -> None:
    """Reject duplicate, missing, forward-unbound, or cyclic event references."""
    by_id = {event.id: event for event in events}
    if len(by_id) != len(events):
        raise CandidateValidationError(
            "Duplicate event IDs",
            phase="compile",
            code="duplicate_event_ids",
            path="/events",
        )
    resolved = set()
    pending = list(events)
    while pending:
        ready = [event for event in pending if set(event.dependencies) <= resolved]
        if not ready:
            raise CandidateValidationError(
                "Cyclic or missing event dependencies",
                phase="compile",
                code="invalid_dependencies",
                path="/events",
            )
        for event in ready:
            refs = set(REFERENCE.findall(str(event.arguments)))
            if not refs <= resolved:
                raise CandidateValidationError(
                    f"Unbound result reference in {event.id}: {refs - resolved}",
                    phase="compile",
                    code="unbound_result_reference",
                    event_id=event.id,
                    field="arguments",
                )
            resolved.add(event.id)
            pending.remove(event)


def compile_series(
    base: Task, draft: SeriesDraft, *, allow_current_evidence=False
) -> tuple[Task, list[dict]]:
    """Preserve the original event graph and scope each new request explicitly."""
    if draft.base_task_id != base.id or draft.base_task_sha256 != digest(base):
        raise ValueError("Candidate does not match the exact sealed base task")
    if base.scenario is None:
        raise ValueError("A Gaia2 task requires a scenario")
    task = base.model_copy(deep=True)
    task.id = f"{base.id}__series{len(draft.stages) + 1}_v1"
    events = task.scenario.events
    index = [
        {
            "stage": 0,
            "event_ids": [e.id for e in events],
            "criteria": base.evaluation_criteria.model_dump(mode="json"),
        }
    ]
    answers = list(base.evaluation_criteria.communicate_info or [])
    assertions = []
    stage_criteria = [base.evaluation_criteria]
    event_stages = {event.id: 0 for event in events}
    for number, stage in enumerate(draft.stages, 1):
        event_stages.update({event.id: number for event in stage.events})
        refs = [
            (f"/evidence/{i}/reference", e.reference)
            for i, e in enumerate(stage.evidence)
        ]
        refs.extend(
            (f"/checks/{i}/{field}", ref)
            for i, check in enumerate(stage.checks)
            for field, ref in [
                ("reference", check.reference),
                ("compare_to", check.compare_to),
            ]
            if ref is not None
        )
        for path, ref in refs:
            if ref.key_from_result:
                producer = ref.key_from_result.event_id
                if producer not in event_stages or event_stages[producer] > ref.stage:
                    raise CandidateValidationError(
                        "Dynamic state key references an unknown or future result",
                        phase="compile",
                        stage=number,
                        code="invalid_dynamic_key_result",
                        path=path + "/key_from_result/event_id",
                        actual=producer,
                    )
        # Recheck mutated/resumed models before constructing any environment.
        for evidence in stage.evidence:
            evidence.reference.valid_pointer()
            evidence.concrete_result_pointer()
        for check in stage.checks:
            check.reference.valid_pointer()
            if check.compare_to is not None:
                check.compare_to.valid_pointer()
        if re.search(r"\b[0-9a-f]{32}\b|\{\{[^{}]+\}\}", stage.request):
            raise ValueError(
                "User requests must not expose internal IDs or result placeholders"
            )
        if stage.criteria.actions or set(stage.criteria.reward_basis) - {
            RewardType.ENV_ASSERTION,
            RewardType.NL_ASSERTION,
            RewardType.COMMUNICATE,
        }:
            raise ValueError("Use native scenario outcomes, not DB/action rewards")
        for assertion in stage.criteria.env_assertions or []:
            if (
                assertion.func_name != "assert_scenario_goal"
                or assertion.arguments != {"source_id": base.scenario.source_id}
                or assertion.env_type != "assistant"
                or assertion.assert_value is not True
            ):
                raise ValueError(
                    "Continuation environment assertions must use the native scenario goal"
                )
        user_id = f"series_s{number}_user"
        previous = {event.id for event in events}
        user = ScenarioAction(
            id=user_id,
            kind="user",
            tool=USER_TOOL,
            arguments={"content": stage.request},
            dependencies=leaves(events),
        )
        added = [user]
        for event_index, event in enumerate(stage.events):
            validate_literal_reference(event.id, "event ID")
            for dependency in event.dependencies:
                validate_literal_reference(dependency, "dependency ID")
            if event.kind != "reference":
                raise CandidateValidationError(
                    "Continuations may only add reference operations; user input is compiled",
                    phase="compile",
                    stage=number,
                    code="invalid_event_kind",
                    path=f"/events/{event_index}/kind",
                )
            if not event.id.startswith(f"series_s{number}_") or event.id == user_id:
                raise CandidateValidationError(
                    f"Use unique series_s{number}_ prefixed event IDs",
                    phase="compile",
                    stage=number,
                    code="invalid_event_id",
                    path=f"/events/{event_index}/id",
                )
            if event.channel == "agent" and event.tool != ANSWER_TOOL:
                raise ValueError("Only reference answers may use the agent channel")
            if event.tool == ANSWER_TOOL and event.channel != "agent":
                raise ValueError("Reference answers require channel=agent")
            event = event.model_copy(deep=True)
            if user_id not in event.dependencies:
                event.dependencies.append(user_id)
            added.append(event)
        events.extend(added)
        try:
            validate_graph(events)
        except CandidateValidationError as error:
            event_id = error.details.get("event_id")
            local_index = next(
                (i for i, e in enumerate(stage.events) if e.id == event_id), None
            )
            error.details["stage"] = number
            if local_index is not None:
                error.details["path"] = (
                    f"/events/{local_index}/{error.details.get('field', 'dependencies')}"
                )
            raise
        # A stage can refer to earlier results, never to a later checkpoint.
        for evidence_index, evidence in enumerate(stage.evidence):
            ref = evidence.reference
            if ref.source != "snapshot" and (
                ref.stage > number
                or (ref.stage == number and not allow_current_evidence)
            ):
                raise CandidateValidationError(
                    "Material evidence must exist before the new request",
                    phase="compile",
                    stage=number,
                    code="future_evidence_checkpoint",
                    path=f"/evidence/{evidence_index}/reference/stage",
                    actual=ref.stage,
                    expected=f"at most {number}"
                    if allow_current_evidence
                    else f"less than {number}",
                )
            available = (
                previous | {e.id for e in stage.events}
                if allow_current_evidence
                else previous
            )
            if ref.source == "result" and ref.event_id not in available:
                raise CandidateValidationError(
                    "Material evidence references an unknown or later operation"
                    if allow_current_evidence
                    else "Material evidence references a future operation",
                    phase="compile",
                    stage=number,
                    code="future_evidence_result",
                    path=f"/evidence/{evidence_index}/reference/event_id",
                    actual=ref.event_id,
                    expected="An existing event from an earlier or current stage"
                    if allow_current_evidence
                    else "An event from a completed earlier stage",
                )
            if allow_current_evidence:
                for producer in REFERENCE.findall(str(evidence.public_arguments)):
                    if producer not in available:
                        raise CandidateValidationError(
                            "Evidence read references an unknown or future operation",
                            phase="compile",
                            stage=number,
                            code="future_evidence_result",
                            path=f"/evidence/{evidence_index}/public_arguments",
                            actual=producer,
                        )
        for check_index, check in enumerate(stage.checks):
            for field, ref in [
                ("reference", check.reference),
                ("compare_to", check.compare_to),
            ]:
                if ref is not None and ref.source != "snapshot" and ref.stage > number:
                    raise CandidateValidationError(
                        "Check references a future checkpoint",
                        phase="compile",
                        stage=number,
                        code="future_check_checkpoint",
                        path=f"/checks/{check_index}/{field}/stage",
                        actual=ref.stage,
                        expected=f"at most {number}",
                    )
        answers.extend(stage.criteria.communicate_info or [])
        stage_criteria.append(stage.criteria)
        index.append(
            {
                "stage": number,
                "request_event": user_id,
                "event_ids": [e.id for e in added],
                **stage.model_dump(mode="json"),
            }
        )
    for number, criteria in enumerate(stage_criteria):
        assertions.extend(
            f"Apply ONLY to stage {number}, ending before the next stage's user request: {text}"
            for text in (criteria.nl_assertions or [])
        )
    basis = [RewardType.ENV_ASSERTION, RewardType.NL_ASSERTION]
    if answers:
        basis.append(RewardType.COMMUNICATE)
    task.evaluation_criteria = EvaluationCriteria(
        actions=[],
        env_assertions=base.evaluation_criteria.env_assertions,
        communicate_info=answers or None,
        nl_assertions=assertions,
        reward_basis=basis,
    )
    instructions = task.user_scenario.instructions
    if not isinstance(instructions, str):
        instructions.task_instructions = (
            "The initial request is supplied verbatim. Only released scripted user "
            "requests may add or revise goals. Respond naturally using released "
            "facts and actually heard speech. Do not invent facts or new tasks. "
            "Future requests, reference answers and application state are private."
        )
    validate_graph(events)
    return task, index

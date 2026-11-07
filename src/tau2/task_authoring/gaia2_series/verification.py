"""Key-free reference execution, grounded evidence, checkpoints, and strict replay."""

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from tau2.data_model.message import ToolCall
from tau2.data_model.tasks import Task
from tau2.task_authoring.gaia2_series.compiler import (
    ANSWER_TOOL,
    REFERENCE,
    compile_series,
)
from tau2.task_authoring.gaia2_series.failures import (
    CandidateValidationError,
    VerificationInfrastructureError,
    candidate_boundary,
    excerpt,
    system_boundary,
)
from tau2.task_authoring.gaia2_series.materials import app_states, pointer_get
from tau2.task_authoring.gaia2_series.schema import SeriesDraft, ValueRef
from tau2.task_authoring.gaia2_series.storage import digest


@dataclass
class Execution:
    """Transient full states; persisted reports store only hashes and evidence."""

    states: dict
    results: dict
    records: list
    checkpoints: list
    initial: dict
    schemas: dict
    delivered_answers: dict = field(default_factory=dict)


def to_public_value(value):
    """Mirror the native tool boundary, which serializes numeric fields as strings."""
    from tau2.environment.environment import Environment

    if isinstance(value, str):
        return value
    return json.loads(Environment.to_json_str(value))


def exposed_equal(actual, expected):
    """Compare public results with source values without changing native runtime types."""
    if (
        isinstance(actual, (int, float, str))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float, str))
        and not isinstance(expected, bool)
    ):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            pass
    if isinstance(actual, str):
        return actual == to_public_value(expected)
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            exposed_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            exposed_equal(a, b) for a, b in zip(actual, expected)
        )
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def resolve_value(ref: ValueRef, execution: Execution):
    with candidate_boundary(
        "value_reference", stage=ref.stage, reference=ref.model_dump(mode="json")
    ):
        return _resolve_value(ref, execution)


def _resolve_value(ref: ValueRef, execution: Execution):
    """Resolve checked evidence without giving it access to unrelated host files."""
    if ref.source == "snapshot":
        value = execution.initial
    elif ref.source == "state":
        value = execution.states[ref.stage]
    else:
        if ref.event_id not in execution.results:
            raise CandidateValidationError(
                f"Unknown result reference: {ref.event_id}",
                phase="value_reference",
                stage=ref.stage,
                event_id=ref.event_id,
            )
        value = execution.results[ref.event_id]
        if value is None and ref.event_id in execution.delivered_answers:
            value = execution.delivered_answers[ref.event_id]
    try:
        value = pointer_get(value, ref.pointer)
        if ref.key_from_result:
            key_ref = ref.key_from_result
            key = pointer_get(execution.results[key_ref.event_id], key_ref.pointer)
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError(
                    "Dynamic state lookup needs a string result key and a dictionary"
                )
            value = pointer_get(value[key], ref.suffix_pointer)
        return value
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise CandidateValidationError(
            f"Invalid {ref.source} pointer {ref.pointer!r}: {error}",
            phase="value_reference",
            stage=ref.stage,
            event_id=ref.event_id,
            pointer=ref.pointer,
            result_preview=excerpt(value),
        ) from None


def reference_goal_met(tools):
    """Fast exact projected equality, with the native entity matcher as fallback.

    Offline reference executions normally reproduce identical generated IDs.
    Avoid graph-isomorphism backtracking over thousands of unchanged records in
    that common case. Both paths retain the native protected-state projection.
    """
    from tau2.domains.gaia2.scoring import (
        _initial_identifiers,
        _project_outcomes,
        business_state,
        check_goal,
        reference_execution,
    )

    tools.refresh_db(include_files=True)
    target, _ = reference_execution(
        tools.source_snapshot, tools.runtime.spec, tools.assets
    )
    identifiers = _initial_identifiers(tools.source_snapshot)
    actual = business_state(
        _project_outcomes(tools.db.apps, tools.source_snapshot, tools.runtime.spec),
        identifiers,
    )
    expected = business_state(
        _project_outcomes(target, tools.source_snapshot, tools.runtime.spec),
        identifiers,
    )
    return actual == expected or check_goal(tools)


def evaluate_check(check, execution: Execution) -> dict:
    """Execute one finite assertion, including independently derived aggregates."""
    actual = resolve_value(check.reference, execution)
    expected = (
        resolve_value(check.compare_to, execution)
        if check.compare_to
        else check.expected
    )
    with candidate_boundary(
        "outcome",
        stage=check.reference.stage,
        check=check.model_dump(mode="json"),
        actual=excerpt(actual),
        expected=excerpt(expected),
    ):
        return _evaluate_check(check, actual, expected)


def _evaluate_check(check, actual, expected):
    operation = check.operation
    if operation == "count":
        actual = len(actual)
    elif operation in {"sum", "min", "max"}:
        actual = {"sum": sum, "min": min, "max": max}[operation](actual)
    if operation == "contains":
        met = expected in actual
    elif operation == "set_equals":
        met = sorted(map(digest, actual)) == sorted(map(digest, expected))
    elif operation == "equals" and check.reference.source == "result":
        met = exposed_equal(actual, expected)
    else:
        met = actual == expected
    if not met:
        raise CandidateValidationError(
            f"Outcome failed: {check.explanation}; actual={str(actual)[:500]}",
            phase="outcome",
            stage=check.reference.stage,
            event_id=check.reference.event_id,
            pointer=check.reference.pointer,
            actual=excerpt(actual),
            expected=excerpt(expected),
        )
    return {"explanation": check.explanation, "actual": actual, "expected": expected}


def _read_evidence(environment, evidence, execution, stage):
    """Require both source identity and an actual public read producing the fact."""
    from tau2.environment.scenario import resolve_references

    actual = resolve_value(evidence.reference, execution)
    if not exposed_equal(actual, evidence.expected):
        raise CandidateValidationError(
            f"Material differs from claimed fact: {evidence.explanation}",
            phase="evidence",
            stage=stage,
            reference=evidence.reference.model_dump(),
            actual=excerpt(actual),
            expected=excerpt(evidence.expected),
            code="evidence_value_mismatch",
            field="reference/pointer",
        )
    toolkit = environment.tools
    operation = toolkit._operations.get(evidence.public_tool)
    if (
        evidence.public_tool not in toolkit.tools
        or operation is None
        or operation.write_operation
    ):
        raise CandidateValidationError(
            f"Evidence requires a public read tool: {evidence.public_tool}",
            phase="evidence",
            stage=stage,
            tool=evidence.public_tool,
            code="invalid_evidence_read",
            field="public_tool",
        )
    args = resolve_references(evidence.public_arguments, execution.results)
    response = environment.get_response(
        ToolCall(
            id=f"evidence_s{stage}_{len(environment.scenario_runtime.records)}",
            name=evidence.public_tool,
            arguments=args,
        )
    )
    if response.error:
        raise CandidateValidationError(
            f"Evidence is not publicly reachable: {response.content}",
            phase="evidence",
            stage=stage,
            tool=evidence.public_tool,
            arguments=args,
            code="public_read_error",
            field="public_arguments",
            actual=excerpt(response.content),
        )
    # ToolMessage content is a serialized result; the journal retains exact JSON.
    result = next(
        record.result
        for record in reversed(environment.scenario_runtime.records)
        if record.kind == "tool" and record.name == evidence.public_tool
    )
    try:
        public_value = pointer_get(result, evidence.result_pointer)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise CandidateValidationError(
            f"Public result pointer is invalid: {evidence.result_pointer!r}: {error}",
            phase="evidence",
            stage=stage,
            tool=evidence.public_tool,
            arguments=args,
            pointer=evidence.result_pointer,
            expected=excerpt(evidence.expected),
            result_preview=excerpt(result),
            code="invalid_public_pointer",
            field="result_pointer",
        ) from None
    if not exposed_equal(public_value, to_public_value(evidence.expected)):
        raise CandidateValidationError(
            f"Public tool does not expose the claimed fact: {evidence.explanation}",
            phase="evidence",
            stage=stage,
            tool=evidence.public_tool,
            pointer=evidence.result_pointer,
            actual=excerpt(public_value),
            expected=excerpt(evidence.expected),
            code="public_value_mismatch",
            field="result_pointer",
            arguments=args,
        )
    return {
        "explanation": evidence.explanation,
        "actual": actual,
        "public_tool": evidence.public_tool,
        "reference": evidence.reference.model_dump(),
    }


def _current_evidence(evidence, stage, event_ids):
    """Solution evidence can depend on reads/writes completed in this stage."""
    ref = evidence.reference
    return (
        (ref.source != "snapshot" and ref.stage == stage)
        or (ref.source == "result" and ref.event_id in event_ids)
        or (
            ref.key_from_result is not None
            and ref.key_from_result.event_id in event_ids
        )
        or bool(set(REFERENCE.findall(str(evidence.public_arguments))) & event_ids)
    )


def _checkpoint_evidence(task, source, execution, runtime, pairs, stage):
    """Read evidence in a disposable checkpoint copy, never the main journal."""
    if not pairs:
        return []
    from tau2.domains.gaia2.environment import get_environment
    from tau2.environment.replay import replay_environment

    with system_boundary("evidence_environment_startup"):
        probe = get_environment(
            task=task, root=source, allow_unverified=True, stage_control=False
        )
    try:
        with system_boundary("evidence_checkpoint_replay"):
            replay_environment(
                probe, [r.model_dump(mode="json") for r in runtime.records]
            )
        rows = []
        for evidence_index, evidence in pairs:
            try:
                with candidate_boundary(
                    "evidence", stage=stage, evidence_index=evidence_index
                ):
                    rows.append(_read_evidence(probe, evidence, execution, stage))
            except CandidateValidationError as error:
                error.details["evidence_index"] = evidence_index
                error.details.setdefault(
                    "path",
                    f"/evidence/{evidence_index}/{error.details.get('field', 'reference/pointer')}",
                )
                raise
        return rows
    finally:
        probe.close()


def execute_reference(
    base: Task,
    source: Path,
    draft: SeriesDraft | None = None,
    *,
    replay=True,
    validation_mode="strict",
) -> Execution:
    """Run one continuous reference episode and verify every stage before advancing."""
    from tau2.domains.gaia2.environment import get_environment
    from tau2.domains.gaia2.utils import read_snapshot, verify_task_artifact
    from tau2.environment.replay import replay_environment
    from tau2.environment.scenario import resolve_references

    if validation_mode not in {"strict", "lightweight"}:
        raise ValueError("validation_mode must be strict or lightweight")
    light = validation_mode == "lightweight"
    with system_boundary("source_validation"):
        verify_task_artifact(Path(source), base)
    with candidate_boundary("compile"):
        task, index = (
            compile_series(base, draft, allow_current_evidence=light)
            if draft is not None
            else (
                base.model_copy(deep=True),
                [{"stage": 0, "event_ids": [e.id for e in base.scenario.events]}],
            )
        )
    with system_boundary("environment_startup"):
        environment = get_environment(
            task=task, root=source, allow_unverified=True, stage_control=False
        )
    replayed = None
    runtime = environment.scenario_runtime
    # Reference preparation is a virtual-clock operation, not an agent simulation.
    execution = Execution(
        {},
        runtime.results,
        [],
        [],
        app_states(read_snapshot(base.scenario.snapshot, Path(source))),
        {},
    )
    from tau2.task_authoring.gaia2_series.materials import public_schema

    execution.schemas = public_schema(environment.tools)
    try:
        if draft:
            for stage in draft.stages:
                for event in stage.events:
                    if (
                        event.tool != ANSWER_TOOL
                        and event.tool not in environment.tools.tools
                    ):
                        raise CandidateValidationError(
                            f"Unknown/private continuation tool: {event.tool}",
                            phase="compile",
                            event_id=event.id,
                            tool=event.tool,
                        )
                    if event.tool.startswith("System__"):
                        raise CandidateValidationError(
                            "New timed waits are outside v1 authoring", phase="compile"
                        )
                known = set()
                for evidence in stage.evidence:
                    known.update(
                        REFERENCE.findall(json.dumps(evidence.public_arguments))
                    )
                if any(
                    event_id not in {e.id for e in task.scenario.events}
                    for event_id in known
                ):
                    raise CandidateValidationError(
                        "Evidence read contains an unknown result reference",
                        phase="compile",
                    )
        completed_stage = -1
        runtime.advance(0)
        while len(runtime.completed) < len(task.scenario.events):
            pending = [
                event
                for event in task.scenario.events
                if event.id not in runtime.completed
            ]
            ready = [
                event
                for event in pending
                if set(event.dependencies) <= runtime.completed.keys()
            ]
            if not ready:
                raise ValueError("Unresolved reference dependencies")
            event = min(ready, key=runtime.event_priority)
            stage_number = next(
                item["stage"] for item in index if event.id in item["event_ids"]
            )
            if event.kind == "user" and stage_number > completed_stage + 1:
                raise ValueError(
                    "A continuation was released before its predecessor completed"
                )
            if event.kind == "user" and stage_number > 0:
                stage = draft.stages[stage_number - 1]
                event_ids = {e.id for e in stage.events}
                pairs = [
                    (i, e)
                    for i, e in enumerate(stage.evidence)
                    if not (light and _current_evidence(e, stage_number, event_ids))
                ]
                evidence_rows = _checkpoint_evidence(
                    task, source, execution, runtime, pairs, stage_number
                )
            else:
                evidence_rows = None
            due, _ = runtime.event_priority(event)
            runtime.advance(max(runtime.elapsed, due))
            if event.id not in runtime.completed:
                with candidate_boundary(
                    "reference_arguments", stage=stage_number, event_id=event.id
                ):
                    arguments = resolve_references(event.arguments, runtime.results)
                if event.kind == "user":
                    runtime.delivered("user", arguments["content"], event_id=event.id)
                elif event.channel == "agent":
                    runtime.delivered("agent", arguments["content"])
                    spoken = next(
                        record.result
                        for record in reversed(runtime.records)
                        if record.kind == "agent"
                    )
                    execution.delivered_answers[event.id] = {"content": spoken}
                elif event.kind == "reference":
                    with candidate_boundary(
                        "reference",
                        stage=stage_number,
                        event_id=event.id,
                        arguments=arguments,
                    ):
                        response = environment.get_response(
                            ToolCall(id=event.id, name=event.tool, arguments=arguments)
                        )
                    if response.error:
                        raise CandidateValidationError(
                            f"{event.tool}: {response.content}",
                            phase="reference",
                            stage=stage_number,
                            event_id=event.id,
                            tool=event.tool,
                            arguments=arguments,
                        )
                if event.id not in runtime.completed:
                    raise VerificationInfrastructureError(
                        f"Reference did not bind: {event.id}"
                    )
            if evidence_rows is not None:
                execution.checkpoints[stage_number - 1]["next_stage_material"] = (
                    evidence_rows
                )
            for item in index:
                number = item["stage"]
                if (
                    number <= completed_stage
                    or not set(item["event_ids"]) <= runtime.completed.keys()
                ):
                    continue
                if number != completed_stage + 1:
                    raise ValueError("Stage checkpoints are out of order")
                environment.tools.refresh_db(include_files=True)
                execution.states[number] = copy.deepcopy(environment.tools.db.apps)
                if light and number:
                    stage = draft.stages[number - 1]
                    event_ids = {e.id for e in stage.events}
                    pairs = [
                        (i, e)
                        for i, e in enumerate(stage.evidence)
                        if _current_evidence(e, number, event_ids)
                    ]
                    rows = _checkpoint_evidence(
                        task, source, execution, runtime, pairs, number
                    )
                    execution.checkpoints[number - 1].setdefault(
                        "next_stage_material", []
                    ).extend(rows)
                checks = []
                if number:
                    for check_index, check in enumerate(
                        draft.stages[number - 1].checks
                    ):
                        try:
                            checks.append(evaluate_check(check, execution))
                        except CandidateValidationError as error:
                            error.details.setdefault(
                                "path",
                                f"/checks/{check_index}/reference/pointer"
                                if error.details.get("phase") == "value_reference"
                                else f"/checks/{check_index}",
                            )
                            error.details.setdefault(
                                "code",
                                "invalid_check_reference"
                                if error.details.get("phase") == "value_reference"
                                else "outcome_check_failed",
                            )
                            raise
                # Prefix target validation supports legitimate later revisions.
                full_spec = runtime.spec
                prefix = full_spec.model_copy(deep=True)
                included = {
                    event_id
                    for part in index[: number + 1]
                    for event_id in part["event_ids"]
                }
                prefix.events = [
                    event for event in full_spec.events if event.id in included
                ]
                runtime.spec = prefix
                try:
                    if not light and not reference_goal_met(environment.tools):
                        raise CandidateValidationError(
                            f"Stage {number} failed its business-state target",
                            phase="outcome",
                            stage=number,
                        )
                finally:
                    runtime.spec = full_spec
                execution.checkpoints.append(
                    {
                        "stage": number,
                        "state_sha256": digest(execution.states[number]),
                        "elapsed": runtime.elapsed,
                        "checks": checks,
                    }
                )
                completed_stage = number
        if not light and draft and len(draft.stages) == 2:
            # Structural evidence of continuity supplements the independent reviewer.
            continuity = any(
                evidence.reference.source in {"state", "result"}
                for stage in draft.stages
                for evidence in stage.evidence
            )
            if not continuity:
                raise CandidateValidationError(
                    "At least one continuation needs evidence from a previous result/state",
                    phase="continuity",
                )
        execution.records = [
            record.model_dump(mode="json") for record in runtime.records
        ]
        if replay:
            with system_boundary("strict_replay"):
                replayed = get_environment(
                    task=task, root=source, allow_unverified=True, stage_control=False
                )
                replay_environment(replayed, execution.records)
            replayed.tools.refresh_db(include_files=True)
            if replayed.tools.db.apps != execution.states[completed_stage]:
                raise VerificationInfrastructureError(
                    "Strict replay did not reproduce the complete final state"
                )
            if not reference_goal_met(replayed.tools):
                raise VerificationInfrastructureError(
                    "Strict replay did not reproduce the business-state target"
                )
        return execution
    finally:
        environment.close()
        if replayed is not None:
            replayed.close()


def verify_draft(
    base: Task,
    source: Path,
    draft: SeriesDraft,
    *,
    replay=True,
    validation_mode="strict",
) -> tuple[dict, Execution]:
    """Return a content-bound successful verification and transient authoring state."""
    execution = execute_reference(
        base, source, draft, replay=replay, validation_mode=validation_mode
    )
    result = {
        "valid": True,
        "content_sha256": digest(draft),
        "base_task_id": base.id,
        "stage_count": len(draft.stages) + 1,
        "checkpoints": execution.checkpoints,
        "replay_verified": replay,
        "validation_mode": validation_mode,
        "journal_sha256": digest(execution.records),
    }
    return result, execution

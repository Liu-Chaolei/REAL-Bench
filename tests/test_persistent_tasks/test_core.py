import json
from pathlib import Path

import pytest

from tau2.domains.gaia2.persistent_tasks import (
    build_task_service,
    build_task_tools,
    compile_persistent_actions,
    load_persistent_scenario,
    validate_source_bindings,
)
from tau2.domains.gaia2.utils import read_snapshot
from tau2.persistent_tasks.adapters import GPTLiveEventAdapter, QwenEventAdapter
from tau2.persistent_tasks.evaluator import evaluate_operations, evaluate_summary
from tau2.persistent_tasks.event_binding import EventBinder
from tau2.persistent_tasks.models import (
    ObservedEvent,
    PersistentTask,
    PersistentTaskRequest,
    ReleaseCondition,
    TaskProgress,
    TaskResult,
    TaskState,
    TaskStateEvent,
)
from tau2.persistent_tasks.scheduler import PersistentTaskScheduler
from tau2.persistent_tasks.trace import TaskTrace
from tau2.persistent_tasks.validation import validate_sidecar

ROOT = Path("data/tau2/domains/gaia2")
SOURCE = "source-task"
STAGE = 1


def task(
    source_task_id=SOURCE, source_stage=STAGE, state=TaskState.WAITING, result=None
):
    return PersistentTask(
        source_task_id=source_task_id,
        source_stage=source_stage,
        title="source task",
        summary="work",
        state=state,
        progress=TaskProgress(current_step="step", completed_steps=0, total_steps=2),
        result=result,
        mutable_fields={"summary"},
    )


def test_service_lifecycle_uses_native_target():
    from tau2.persistent_tasks.service import PersistentTaskService

    result = TaskResult(summary="done")
    service = PersistentTaskService([task()])
    service.apply_event(
        TaskStateEvent(
            id="start",
            source_task_id=SOURCE,
            source_stage=STAGE,
            new_state=TaskState.RUNNING,
        )
    )
    assert service.query_task(SOURCE, STAGE).state == TaskState.RUNNING
    assert (
        service.modify_task(SOURCE, STAGE, {"summary": "updated"}).summary == "updated"
    )
    assert service.cancel_task(SOURCE, STAGE).state == TaskState.CANCELLED
    with pytest.raises(ValueError, match="cannot modify terminal"):
        service.modify_task(SOURCE, STAGE, {"summary": "late"})
    completed = PersistentTaskService([task("source2", 2, TaskState.RUNNING)])
    completed.apply_event(
        TaskStateEvent(
            id="done",
            source_task_id="source2",
            source_stage=2,
            new_state=TaskState.COMPLETED,
            result=result,
            progress=TaskProgress(
                current_step="done", completed_steps=2, total_steps=2
            ),
        )
    )
    assert completed.get_task_result("source2", 2).summary == "done"
    with pytest.raises(ValueError, match="cannot cancel completed"):
        completed.cancel_task("source2", 2)


def test_scheduler_releases_after_original_event():
    from tau2.persistent_tasks.service import PersistentTaskService

    service = PersistentTaskService([task()])
    scheduler = PersistentTaskScheduler(
        service,
        [
            TaskStateEvent(
                id="progress",
                source_task_id=SOURCE,
                source_stage=STAGE,
                new_state=TaskState.RUNNING,
                dependencies=["original-event"],
                delay_seconds=5,
            )
        ],
        [
            PersistentTaskRequest(
                request_id="r1",
                text="status?",
                release_condition=ReleaseCondition(
                    source_task_id=SOURCE, source_stage=STAGE, state=TaskState.RUNNING
                ),
            )
        ],
    )
    assert scheduler.advance(elapsed=4) == []
    assert scheduler.on_event_completed("original-event", timestamp=5) == []
    released = scheduler.advance(elapsed=10)
    assert [item.request_id for item in released] == ["r1"]
    assert service.query_task(SOURCE, STAGE).state == TaskState.RUNNING


def test_sidecar_samples_bind_native_source_events():
    manifest = json.loads((ROOT / "persistent_tasks/manifest.json").read_text())
    assert manifest["scenarios"] == 10
    scenario = load_persistent_scenario("pt_0001", root=ROOT)
    service = build_task_service(scenario)
    task_view = service.list_tasks()[0]
    assert task_view.source_task_id == scenario["task"]["source_task_id"]
    assert task_view.source_stage == scenario["task"]["source_stage"]
    assert "setup" not in scenario["task"]
    validate_source_bindings(scenario, root=ROOT)
    source = next(
        item
        for item in json.loads((ROOT / "tasks.json").read_text())
        if item["id"] == scenario["task"]["source_task_id"]
    )
    actions = compile_persistent_actions(scenario, source_task=source)
    assert actions[-1].kind == "user"
    assert set(actions[-1].dependencies) <= set(
        scenario["task"]["source_event_ids"]
    ) | {action.id for action in actions}


def test_gaia2_task_toolkit_exposes_lifecycle_tools():
    scenario = load_persistent_scenario("pt_0001", root=ROOT)
    source = next(
        item
        for item in json.loads((ROOT / "tasks.json").read_text())
        if item["id"] == scenario["task"]["source_task_id"]
    )
    tools = build_task_tools(
        read_snapshot(source["scenario"]["snapshot"], ROOT),
        assets=ROOT / "assets",
        scenario=scenario,
    )
    assert {
        "list_tasks",
        "query_task",
        "modify_task",
        "cancel_task",
        "get_task_result",
    } <= set(tools.get_tools())
    tools.close()


def test_operation_evaluator_requires_native_target():
    task_view = task(state=TaskState.RUNNING)
    trace = TaskTrace()
    trace.operation(
        request_id="r1",
        operation="query",
        source_task_id=SOURCE,
        source_stage=STAGE,
        state_before=TaskState.RUNNING,
        state_after=TaskState.RUNNING,
        result=task_view,
    )
    assert (
        evaluate_operations(
            [
                {
                    "request_id": "r1",
                    "operation": "query",
                    "source_task_id": SOURCE,
                    "source_stage": STAGE,
                }
            ],
            trace.operations,
        )["reward"]
        == 1.0
    )
    assert (
        evaluate_operations(
            [
                {
                    "request_id": "r1",
                    "operation": "query",
                    "source_task_id": "other",
                    "source_stage": STAGE,
                }
            ],
            trace.operations,
        )["reward"]
        == 0.0
    )


def test_operation_evaluator_rejects_duplicate_request_ids():
    records = [
        {
            "request_id": "r1",
            "operation": "query",
            "source_task_id": SOURCE,
            "source_stage": STAGE,
        },
        {
            "request_id": "r1",
            "operation": "query",
            "source_task_id": SOURCE,
            "source_stage": STAGE,
        },
    ]
    result = evaluate_operations(
        [
            {
                "request_id": "r1",
                "operation": "query",
                "source_task_id": SOURCE,
                "source_stage": STAGE,
            }
        ],
        records,
    )
    assert result["reward"] == 0.0
    assert result["duplicate_request_ids"] == ["r1"]


def test_service_attributes_unlabelled_operation_to_active_request():
    from tau2.persistent_tasks.service import PersistentTaskService

    service = PersistentTaskService([task(state=TaskState.RUNNING)])
    service.set_active_request("r1")
    service.query_task(SOURCE, STAGE)
    assert service.trace.operations[-1].request_id == "r1"


def test_event_binder_requires_one_matching_native_event():
    from tau2.data_model.scenario import ScenarioAction

    event = ScenarioAction(
        id="native-1",
        kind="reference",
        channel="tool",
        tool="Tasks__query",
        arguments={"task_id": "source"},
    )
    bound = []
    binder = EventBinder([event], on_bound=lambda *args: bound.append(args))
    result = binder.bind(
        ObservedEvent(
            provider="test",
            kind="tool_call_completed",
            timestamp=3,
            tool_name="Tasks__query",
            arguments={"task_id": "source"},
            result={"state": "running"},
            accepted=True,
        )
    )
    assert result.status == "bound"
    assert result.event_id == "native-1"
    assert bound[0][0] == "native-1"


def test_scheduler_waits_for_turn_boundary_before_release():
    from tau2.persistent_tasks.service import PersistentTaskService

    service = PersistentTaskService([task()])
    scheduler = PersistentTaskScheduler(
        service,
        [
            TaskStateEvent(
                id="progress",
                source_task_id=SOURCE,
                source_stage=STAGE,
                new_state=TaskState.RUNNING,
                dependencies=["original-event"],
            )
        ],
        [
            PersistentTaskRequest(
                request_id="r1",
                text="status?",
                release_condition=ReleaseCondition(
                    source_task_id=SOURCE,
                    source_stage=STAGE,
                    state=TaskState.RUNNING,
                ),
            )
        ],
    )
    assert scheduler.on_event_completed("original-event", timestamp=5) == []
    assert scheduler.on_turn_boundary(timestamp=5)[0].request_id == "r1"


def test_scheduler_applies_release_delay_after_milestone():
    from tau2.persistent_tasks.service import PersistentTaskService

    service = PersistentTaskService([task()])
    scheduler = PersistentTaskScheduler(
        service,
        [
            TaskStateEvent(
                id="progress",
                source_task_id=SOURCE,
                source_stage=STAGE,
                new_state=TaskState.RUNNING,
                dependencies=["original-event"],
            )
        ],
        [
            PersistentTaskRequest(
                request_id="r1",
                text="status?",
                release_condition=ReleaseCondition(
                    source_task_id=SOURCE,
                    source_stage=STAGE,
                    state=TaskState.RUNNING,
                    delay_seconds=1,
                ),
            )
        ],
    )
    assert scheduler.on_event_completed("original-event", timestamp=5) == []
    assert scheduler.on_turn_boundary(timestamp=5) == []
    assert scheduler.on_turn_boundary(timestamp=6)[0].request_id == "r1"


def test_provider_adapters_preserve_backend_identity_and_kind():
    qwen = QwenEventAdapter().convert(
        {"type": "tool_result", "name": "Tasks__query", "arguments": {}}
    )
    live = GPTLiveEventAdapter().convert({"type": "response.done", "timestamp": 2})
    assert (qwen.provider, qwen.kind) == ("qwen", "tool_call_completed")
    assert (live.provider, live.kind) == ("gpt_live", "backend_update")


def test_serialized_summary_grades_operations_and_replies():
    result = evaluate_summary(
        {
            "expected_operations": [
                {
                    "request_id": "r1",
                    "operation": "query",
                    "source_task_id": SOURCE,
                    "source_stage": STAGE,
                    "expected_milestone": "m1",
                }
            ],
            "expected_checks": [{"request_id": "r1", "check_reply": True}],
            "replies": {"r1": "running"},
            "trace": {
                "operations": [
                    {
                        "request_id": "r1",
                        "operation": "query",
                        "source_task_id": SOURCE,
                        "source_stage": STAGE,
                        "state_before": "running",
                        "state_after": "running",
                        "result": {"completed_milestones": ["m1"]},
                    }
                ]
            },
        }
    )
    assert result["reward"] == 1.0


def test_draft_sidecar_has_valid_native_bindings():
    assert validate_sidecar(ROOT / "persistent_tasks") == []

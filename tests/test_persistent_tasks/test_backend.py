from tau2.persistent_tasks.adapters.qwen import QwenEventAdapter
from tau2.persistent_tasks.backend import (
    BackendRequest,
    BackendTaskObservation,
    BackendTaskProjector,
    BackendTriggerScheduler,
    CanonicalState,
    TaskIdentity,
    TaskIdentityBinder,
)
from tau2.persistent_tasks.evaluator import evaluate_backend_operations


def test_backend_state_is_projected_without_synthetic_task_tools():
    projector = BackendTaskProjector()
    projector.bind(TaskIdentity("primary", "source", 1, "backend-1"))
    transition = projector.ingest(
        BackendTaskObservation(
            provider="qwen",
            kind="task_state_changed",
            timestamp=2,
            backend_task_id="backend-1",
            status="running",
        )
    )
    assert transition is not None
    assert transition.current == CanonicalState.RUNNING


def test_trigger_uses_projected_state_and_delay():
    projector = BackendTaskProjector()
    projector.bind(TaskIdentity("primary", "source", 1, "backend-1"))
    scheduler = BackendTriggerScheduler(
        [
            BackendRequest(
                "request_1",
                "status?",
                "source",
                1,
                CanonicalState.RUNNING,
                delay_seconds=1,
            )
        ]
    )
    transition = projector.ingest(
        BackendTaskObservation(
            provider="qwen",
            kind="task_state_changed",
            timestamp=2,
            backend_task_id="backend-1",
            status="running",
        )
    )
    assert transition is not None
    scheduler.ingest(transition)
    assert scheduler.release(timestamp=2.5, turn_boundary=True) == []
    assert scheduler.release(timestamp=3, turn_boundary=False) == []
    assert [
        item.request_id for item in scheduler.release(timestamp=3, turn_boundary=True)
    ] == ["request_1"]


def test_response_done_does_not_project_completion_without_task_status():
    projector = BackendTaskProjector()
    projector.bind(TaskIdentity("primary", "source", 1, "backend-1"))
    transition = projector.ingest(
        BackendTaskObservation(
            provider="gpt_live",
            kind="task_state_changed",
            timestamp=2,
            backend_task_id="backend-1",
            status=None,
        )
    )
    assert transition is None or transition.current == CanonicalState.UNKNOWN


def test_native_backend_operation_is_matched_strictly():
    result = evaluate_backend_operations(
        [
            {
                "request_id": "request_1",
                "operation": "query",
                "source_task_id": "source",
                "source_stage": 1,
            }
        ],
        [
            BackendTaskObservation(
                provider="gpt_live",
                kind="tool_call_completed",
                timestamp=3,
                request_id="request_1",
                operation="query",
                source_task_id="source",
                source_stage=1,
            )
        ],
    )
    assert result["reward"] == 1.0


def test_qwen_backend_task_state_payload_maps_without_guessing_status():
    observation = QwenEventAdapter().backend_observation(
        {
            "type": "backend_task_state",
            "task_id": "qwen-1",
            "state": "running",
            "time": 4,
        }
    )
    assert observation.backend_task_id == "qwen-1"
    assert observation.status == "running"
    submit = QwenEventAdapter().backend_observation(
        {"type": "diagnostic_backend", "role": "submit", "task_id": "qwen-1"}
    )
    assert submit.status is None


def test_identity_binder_uses_declared_source_task_and_stage():
    binder = TaskIdentityBinder(
        [{"role": "primary", "source_task_id": "source", "source_stage": 1}]
    )
    binder.active_stage = 1
    identity = binder.bind_observation(
        BackendTaskObservation(
            provider="qwen",
            kind="task_created",
            timestamp=1,
            backend_task_id="qwen-1",
            status="running",
        )
    )
    assert identity.source_task_id == "source"
    assert identity.source_stage == 1

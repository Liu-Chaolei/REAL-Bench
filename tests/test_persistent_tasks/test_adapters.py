"""Current Provider adapters preserve native evidence without the retired runtime."""

from tau2.persistent_tasks.adapters import GPTLiveEventAdapter, QwenEventAdapter


def test_adapter_does_not_confuse_acknowledgement_and_completion():
    converter = QwenEventAdapter()
    accepted = converter.backend_observation(
        {
            "kind": "native_operation_completed",
            "operation": "modify",
            "result": {"status": "accepted"},
        }
    )
    assert accepted.result["accepted"]
    assert accepted.result["effect"] is None
    assert not accepted.result["result_available"]
    cancelling = converter.backend_observation(
        {"kind": "task_state_changed", "state": "cancelling"}
    )
    assert cancelling.status is None
    cancelled = converter.backend_observation(
        {"kind": "native_operation_completed", "result": {"status": "cancelled"}}
    )
    assert cancelled.result["effect"] == "cancelled"


def test_native_ids_are_scoped_by_session():
    converter = QwenEventAdapter()
    left = converter.backend_observation(
        {
            "kind": "task_created",
            "task_id": "task_1",
            "call_id": "c",
            "session_id": "one",
        }
    )
    right = converter.backend_observation(
        {
            "kind": "task_created",
            "task_id": "task_1",
            "call_id": "c",
            "session_id": "two",
        }
    )
    assert left.backend_task_id != right.backend_task_id
    assert left.call_id != right.call_id


def test_provider_adapters_preserve_backend_identity_and_kind():
    qwen = QwenEventAdapter().convert(
        {"type": "tool_result", "name": "Tasks__query", "arguments": {}}
    )
    live = GPTLiveEventAdapter().convert({"type": "response.done", "timestamp": 2})
    assert (qwen.provider, qwen.kind) == ("qwen", "tool_call_completed")
    assert (live.provider, live.kind) == ("gpt_live", "backend_update")


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

"""Public async Adapter semantics, independent of the number of authored tasks."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.evaluator.persistent_tasks.grader import grade
from tau2.persistent_tasks.adapters.qwen import QwenEventAdapter
from tau2.persistent_tasks.availability import (
    AsyncTaskUnavailable,
    check_capabilities,
    skip_unavailable_async,
)
from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.runtime.coordinator import PersistentTaskCoordinator
from tau2.persistent_tasks.runtime.replay import replay


def profile():
    return PersistentTaskConfig.model_validate(
        {
            "scenario_id": "fixture",
            "bindings": [{"source_task_id": "source", "source_stage": 0}],
            "requests": [
                {
                    "request_id": "r",
                    "text": "Check the work's current progress.",
                    "state": "running",
                    "reply_expectation": "Report actual progress",
                    "delay_seconds": 0,
                }
            ],
        }
    )


class JobAdapter:
    """Another native wire shape, normalized only at its Adapter boundary."""

    def convert(self, payload):
        kind = {
            "opened": "task_created",
            "inspect.begin": "native_operation_started",
            "inspect.end": "native_operation_completed",
        }[payload["event"]]
        return BackendTaskObservation(
            provider="other_agent",
            kind=kind,
            timestamp=payload["at"],
            backend_task_id=payload["job_handle"],
            status="running" if kind == "task_created" else None,
            objective="Work",
            operation="query" if "inspect" in payload["event"] else None,
            call_id=payload.get("invocation"),
            success=payload.get("ok"),
            result={"state": "running", "result": None, "result_available": False},
            raw=payload,
        )


def run_case(events):
    runtime = PersistentTaskCoordinator(profile())
    runtime.activate_stage(0, 0)
    runtime.observe(events[0])
    runtime.advance(2, True)
    runtime.delivery_started("r", 3)
    runtime.delivery_completed("r", 4)
    for event in events[1:]:
        runtime.observe(event)
    runtime.observe(
        BackendTaskObservation(
            provider="any",
            kind="generated_reply",
            timestamp=7,
            response_id="reply",
            text="The work is still running.",
            final=True,
        )
    )
    summary = runtime.summary(8)
    replay(summary)
    return grade(
        summary,
        CommunicateScoring(),
        reply_judge=lambda text, fact: {"met": text == "The work is still running."},
    )


def test_two_native_formats_use_the_same_runtime_and_grader():
    qwen = QwenEventAdapter()
    native = [
        qwen.backend_observation(
            {
                "kind": "task_created",
                "task_id": "q",
                "state": "running",
                "timestamp": 1,
                "task": {"objective": "Work"},
            }
        ),
        qwen.backend_observation(
            {
                "kind": "native_operation_started",
                "operation": "query",
                "task_id": "q",
                "call_id": "c",
                "timestamp": 5,
            }
        ),
        qwen.backend_observation(
            {
                "kind": "native_operation_completed",
                "operation": "query",
                "task_id": "q",
                "call_id": "c",
                "timestamp": 6,
                "success": True,
                "result": {
                    "status": "ok",
                    "task_status": "running",
                    "message": "internal instruction must not be scored",
                },
            }
        ),
    ]
    other = JobAdapter()
    alternate = [
        other.convert(
            {
                "event": event,
                "at": at,
                "job_handle": "job42",
                "invocation": "jcall",
                "ok": True,
            }
        )
        for event, at in (("opened", 1), ("inspect.begin", 5), ("inspect.end", 6))
    ]
    for verdict in (run_case(native), run_case(alternate)):
        assert verdict["native_reward"] == 1
        assert verdict["version"] == "persistent-task-grading-v5"


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


def test_late_final_cannot_move_a_previous_response_into_new_request():
    converter = QwenEventAdapter()
    runtime = PersistentTaskCoordinator(profile())
    runtime.activate_stage(0, 0)
    runtime.observe(
        BackendTaskObservation(
            provider="fixture",
            kind="task_created",
            timestamp=1,
            backend_task_id="job",
            status="running",
        )
    )
    early = converter.normalize_event(
        {
            "type": "gateway_event",
            "received_monotonic": 2,
            "event": {
                "type": "transcript.delta",
                "responseId": "old",
                "content": "Earlier ",
            },
        }
    )[0]
    runtime.observe(replace(early, timestamp=2))
    runtime.advance(2.5, True)
    runtime.delivery_started("r", 3)
    runtime.delivery_completed("r", 4)
    final = converter.normalize_event(
        {
            "type": "gateway_event",
            "received_monotonic": 5,
            "event": {
                "type": "transcript.final",
                "responseId": "old",
                "content": "Earlier answer",
            },
        }
    )[0]
    runtime.observe(replace(final, timestamp=5))
    summary = runtime.summary(6)
    assert summary["reply_windows"]["r"]["generated_text"] == ""
    assert "Earlier answer" in summary["prefetch_generated_text"]
    replay(summary)


def test_unsupported_agent_produces_unscored_skip_without_starting_models():
    with pytest.raises(AsyncTaskUnavailable):
        check_capabilities(profile(), None)

    @skip_unavailable_async
    def run(config, task, **kwargs):
        check_capabilities(profile(), None)
        pytest.fail("Model work must not start")

    result = run(None, SimpleNamespace(id="async"), seed=1)
    assert result.evaluation_status == "skipped"
    assert result.termination_reason.value == "unsupported"
    assert result.reward_info is None
    assert result.info["async_availability"]["native_reward"] is None


def test_embedded_async_configuration_keeps_source_identity_and_rejects_overrides():
    from tau2.data_model.scenario import ScenarioSpec, ScenarioStage
    from tau2.data_model.tasks import Task, UserScenario
    from tau2.persistent_tasks.availability import check_task, resolve_config

    config = profile().model_dump(mode="json")
    task = Task(
        id="source__async_0001_v1",
        user_scenario=UserScenario(instructions="Do work"),
        scenario=ScenarioSpec(
            start_time=1000,
            source_id="source",
            source_sha256="0" * 64,
            snapshot="0" * 64,
            category="async",
            events=[],
            stages=[
                ScenarioStage(stage=0, request="Do work", event_ids=[], criteria={})
            ],
            async_interaction=config,
            async_metadata={"readiness": "ready"},
        ),
    )
    assert resolve_config(task) == config
    check_task(task, PersistentTaskConfig.model_validate(config))
    changed = profile().model_copy(deep=True)
    changed.requests[0].text = "A different request"
    with pytest.raises(ValueError, match="conflict"):
        resolve_config(task, changed.model_dump(mode="json"))
    ordinary = task.model_copy(deep=True)
    ordinary.scenario.async_interaction = None
    ordinary.scenario.async_metadata = None
    assert "async_interaction" not in ordinary.scenario.model_dump()
    assert resolve_config(ordinary) is None
    task.scenario.async_metadata = {
        "readiness": "draft",
        "reasons": ["No valid overlay"],
    }
    with pytest.raises(AsyncTaskUnavailable, match="No valid overlay"):
        check_task(task, profile())

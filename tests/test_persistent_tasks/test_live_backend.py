"""Failure contracts and frozen native evidence replay without live model calls."""

from dataclasses import replace

import pytest

from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.persistent_tasks.backend import (
    BackendTaskObservation,
    BackendTaskProjector,
    CanonicalState,
    TaskIdentity,
    TaskIdentityBinder,
)
from tau2.persistent_tasks.backend_runtime import BackendTaskRuntime, replay
from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.grading import grade


def config(**request):
    return PersistentTaskConfig.model_validate(
        {
            "scenario_id": "test",
            "bindings": [{"source_task_id": "source", "source_stage": 0}],
            "requests": [
                {
                    "request_id": "r1",
                    "text": "Status?",
                    "state": "running",
                    "delay_seconds": 1,
                    "reply_expectation": "Report actual status",
                    **request,
                }
            ],
        }
    )


def event(kind="task_state_changed", status="running", timestamp=2, **kwargs):
    from tau2.persistent_tasks.adapters.qwen import normalize_result

    if kind.startswith("native_operation_") and "result" in kwargs:
        kwargs["result"] = normalize_result(kwargs["result"])
    if (kwargs.get("arguments") or {}).get("list_all"):
        kwargs["purpose"] = "task_discovery"
    if kwargs.get("raw"):
        kwargs["objective"] = kwargs["raw"].get("task", {}).get("objective")
    return BackendTaskObservation(
        provider="qwen",
        kind=kind,
        timestamp=timestamp,
        backend_task_id="native",
        status=status,
        **kwargs,
    )


def test_binding_requires_creation_activation_and_unique_candidate():
    rules = [
        {
            "source_task_id": "source",
            "source_stage": 0,
            "activation_event_ids": ["started"],
        }
    ]
    binder = TaskIdentityBinder(rules)
    assert binder.bind_observation(event("task_created")) is None
    binder.active_stage = 0
    assert binder.bind_observation(event("task_created")) is None
    binder.completed_events.add("started")
    assert binder.bind_observation(event()) is None
    assert binder.bind_observation(event("task_created")).source_task_id == "source"
    assert (
        binder.bind_observation(replace(event("task_created"), backend_task_id=None))
        is None
    )


def test_ambiguous_roles_and_second_task_are_rejected():
    rules = [
        {"source_task_id": "source", "source_stage": 0, "role": r}
        for r in ("one", "two")
    ]
    binder = TaskIdentityBinder(rules)
    binder.active_stage = 0
    assert binder.bind_observation(event("task_created")) is None
    assert binder.issues[-1]["reason"] == "ambiguous_binding"
    binder = TaskIdentityBinder(rules[:1])
    binder.active_stage = 0
    assert binder.bind_observation(event("task_created"))
    assert (
        binder.bind_observation(
            replace(event("task_created"), backend_task_id="another")
        )
        is None
    )


def test_projector_ignores_diagnostics_and_protects_terminal_order():
    projector = BackendTaskProjector()
    assert projector.ingest(event()) is None
    projector.bind(TaskIdentity("primary", "source", 0, "native"))
    assert projector.ingest(event(sequence=2)).current == CanonicalState.RUNNING
    assert projector.ingest(event("diagnostic_backend", status=None)) is None
    assert projector.ingest(event(status="completed", timestamp=3, sequence=3)) is None
    assert (
        projector.ingest(
            event(
                status="completed",
                timestamp=4,
                sequence=4,
                result_available=True,
                result="",
            )
        ).current
        == CanonicalState.COMPLETED
    )
    assert projector.ingest(event(timestamp=1, sequence=1)) is None
    assert projector.ingest(event(timestamp=5, sequence=5)) is None
    assert projector.states["native"] == CanonicalState.COMPLETED


def test_fast_completion_expires_running_trigger_without_faking_a_request():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    assert runtime.advance(1.5, True) == []
    runtime.observe(
        event(status="completed", timestamp=1.7, result_available=True, result="done")
    )
    assert runtime.advance(3, True) == []
    assert runtime.scheduler.rows["r1"]["reason"] == "expired_state"


def test_real_native_sequence_replays_and_grades_without_nonempty_reply_shortcut():
    runtime = BackendTaskRuntime(
        config(request_operation_policy="allow_fresh_prefetch")
    )
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1, event_id="create"))
    assert runtime.advance(2, False) == []
    assert len(runtime.advance(2, True)) == 1
    runtime.delivery_started("r1", 2.5)
    runtime.delivery_completed("r1", 3)
    runtime.observe(
        event(
            "native_operation_started", timestamp=4, call_id="call", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=5,
            call_id="call",
            operation="query",
            success=True,
            result={"status": "ok", "task_id": "native", "task_status": "running"},
        )
    )
    runtime.agent_delivery_seen(6)
    runtime.add_reply("It is still running.", 5.5, 6)
    runtime.add_generated_text("It is still running.", 5.5, 6, response_id="reply")
    runtime.advance(7, True)
    summary = runtime.summary(8)
    assert replay(summary)["operations"] == summary["operations"]
    scoring = CommunicateScoring.model_validate({})
    result = grade(
        summary,
        scoring,
        reply_judge=lambda text, fact: {"met": "still running" in text},
    )
    assert result["completion_score"] == 1
    assert result["evaluation_log"]["coverage"]["delivered"] == 1
    assert (
        grade(summary, scoring, reply_judge=lambda *_: {"met": False})[
            "completion_score"
        ]
        == 0
    )
    summary["operations"][0]["success"] = False
    with pytest.raises(ValueError, match="hash"):
        replay(summary)


def test_unasked_request_is_a_failure_with_explicit_coverage():
    runtime = BackendTaskRuntime(config())
    result = grade(runtime.summary(8), CommunicateScoring.model_validate({}))
    assert result["completion_score"] is None
    assert result["status"] == "not_reached"
    assert result["evaluation_log"]["coverage"]["expected"] == 1
    assert result["evaluation_log"]["coverage"]["delivered"] == 0


def test_result_keeps_the_request_of_its_call_start():
    runtime = BackendTaskRuntime(config())
    runtime.active_request = "r1"
    runtime.observe(
        event("native_operation_started", call_id="call", operation="query")
    )
    runtime.active_request = "later"
    runtime.observe(
        event("native_operation_completed", timestamp=3, call_id="call", success=True)
    )
    assert runtime.operations["call"]["request_id"] == "r1"


def test_separate_result_confirms_pending_completion_without_reopening_terminal():
    projector = BackendTaskProjector()
    projector.bind(TaskIdentity("primary", "source", 0, "native"))
    projector.ingest(event(sequence=1))
    assert projector.ingest(event(status="completed", timestamp=3, sequence=2)) is None
    confirmed = projector.ingest(
        event(
            kind="task_result",
            status=None,
            timestamp=4,
            sequence=3,
            result_available=True,
            result=None,
        )
    )
    assert confirmed.current == CanonicalState.COMPLETED


def test_internal_tasks_and_unobservable_request_states_fail_closed():
    binder = TaskIdentityBinder([{"source_task_id": "source", "source_stage": 0}])
    binder.active_stage = 0
    assert (
        binder.bind_observation(event("task_created", parent_task_id="parent")) is None
    )
    with pytest.raises(ValueError, match="cannot trigger"):
        config(state="unknown")


def test_next_source_stage_closes_operation_attribution_and_reply_window():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.activate_stage(1, 8)
    runtime.add_reply("A reply for a different stage", 9, 10)
    runtime.observe(
        event(
            "native_operation_started", timestamp=11, call_id="other", operation="query"
        )
    )
    summary = runtime.summary(20)
    assert summary["operations"][0]["request_id"] is None
    assert summary["reply_windows"]["r1"]["end"] == 8
    assert summary["reply_windows"]["r1"]["text"] == ""
    assert replay(summary)["reply_windows"] == summary["reply_windows"]


def test_private_scenario_config_cannot_enable_other_provider_or_language():
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import TextRunConfig, VoiceRunConfig

    with pytest.raises(ValueError, match="requires English Gaia2"):
        TextRunConfig(
            domain="gaia2", persistent_task_config=config().model_dump(mode="json")
        )
    with pytest.raises(ValueError, match="requires English Gaia2"):
        VoiceRunConfig(
            domain="gaia2",
            audio_native_config=AudioNativeConfig(),
            persistent_task_config=config().model_dump(mode="json"),
        )
    assert "persistent_task_config" not in TextRunConfig(domain="mock").model_dump(
        mode="json"
    )


def test_later_permission_update_is_not_a_duplicate_progress_query_or_terminal_conflict():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.observe(
        event(
            "native_operation_started", timestamp=5, call_id="query", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=6,
            call_id="query",
            operation="query",
            success=True,
            result={"status": "ok", "task_status": "running"},
        )
    )
    runtime.add_reply("It is running, awaiting approval.", 6, 7)
    runtime.add_generated_text(
        "It is running, awaiting approval.", 6, 7, response_id="reply"
    )
    runtime.agent_delivery_seen(7)
    runtime.advance(7.5, True)
    runtime.ordinary_user_started(8)
    runtime.observe(
        event(status="completed", timestamp=9, result_available=True, result="done")
    )
    runtime.observe(event(status="running", timestamp=10))
    runtime.observe(
        event(
            "native_operation_started",
            timestamp=11,
            call_id="permission",
            operation="modify",
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=12,
            call_id="permission",
            operation="modify",
            success=True,
        )
    )
    runtime.add_reply("I submitted your new instruction.", 12, 13)
    summary = runtime.summary(14)
    assert not summary["issues"]
    assert summary["reply_windows"]["r1"]["end"] == 8
    assert summary["operations"][1]["request_id"] is None
    assert replay(summary)["operations"] == summary["operations"]
    verdict = grade(
        summary,
        CommunicateScoring.model_validate({}),
        reply_judge=lambda *_: {"met": True},
    )
    assert verdict["completion_score"] == 1


def test_completed_task_does_not_become_a_failed_model_answer_when_trigger_was_unasked():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.observe(
        event(status="completed", timestamp=1.1, result_available=True, result="done")
    )
    result = grade(runtime.summary(5), CommunicateScoring.model_validate({}))
    assert result["status"] == "not_reached"
    assert result["completion_score"] is None
    assert result["evaluation_log"]["coverage"]["delivery_started"] == 0


def test_native_role_keywords_match_case_without_guessing_an_ambiguous_role():
    binder = TaskIdentityBinder(
        [
            {
                "source_task_id": "source",
                "source_stage": 0,
                "role": "orders",
                "objective_contains": "order",
            }
        ]
    )
    binder.active_stage = 0
    bound = binder.bind_observation(
        event("task_created", raw={"task": {"objective": "ORDER the requested toys"}})
    )
    assert bound.role == "orders"


def test_frozen_reply_verdict_can_be_reused_when_only_counting_policy_changes(
    monkeypatch,
):
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.observe(
        event(
            "native_operation_started", timestamp=5, call_id="query", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=6,
            call_id="query",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    runtime.add_reply("Still running.", 6, 7)
    runtime.add_generated_text("Still running.", 6, 7, response_id="reply")
    summary = runtime.summary(9)
    scoring = CommunicateScoring.model_validate({})
    summary["grading"] = grade(
        summary,
        scoring,
        reply_judge=lambda text, fact: {
            "met": True,
            "info": fact,
            "justification": "Frozen native result check",
        },
    )
    summary["grading"]["cache_key"] = "previous-policy-key"
    monkeypatch.setattr(
        "tau2.evaluator.persistent_tasks.grader.CommunicateEvaluator.evaluate_communicate_info",
        lambda *a, **kw: pytest.fail(
            "Identical reply input must reuse its frozen verdict"
        ),
    )
    assert grade(summary, scoring)["completion_score"] == 1


def test_fresh_prefetch_is_attributed_to_request_without_rewriting_operation_time():
    runtime = BackendTaskRuntime(
        config(request_operation_policy="allow_fresh_prefetch")
    )
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1, event_id="create"))
    runtime.observe(
        event(
            "native_operation_started",
            timestamp=2,
            call_id="prefetch",
            operation="query",
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=3,
            call_id="prefetch",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    runtime.advance(3.5, True)
    runtime.delivery_started("r1", 4)
    record = runtime.operations["prefetch"]
    assert record["request_id"] == "r1"
    assert record["attribution"] == "prefetch"
    assert record["attribution_valid"] is True
    assert record["started_at"] == 2
    assert record["request_delivery_started_at"] == 4


def test_prefetch_is_rejected_after_state_changes_or_when_policy_is_strict():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.observe(
        event(
            "native_operation_started",
            timestamp=2,
            call_id="prefetch",
            operation="query",
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=3,
            call_id="prefetch",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    runtime.observe(
        event(status="completed", timestamp=3.5, result_available=True, result="done")
    )
    runtime.advance(4, True)
    runtime.delivery_started("r1", 5)
    assert runtime.operations["prefetch"]["request_id"] is None
    assert runtime.operations["prefetch"]["attribution_valid"] is False


def test_post_delivery_query_supersedes_prefetch_for_operation_scoring():
    runtime = BackendTaskRuntime(
        config(request_operation_policy="allow_fresh_prefetch")
    )
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    for kind in ("native_operation_started", "native_operation_completed"):
        runtime.observe(
            event(
                kind,
                timestamp=2 if kind.endswith("started") else 2.1,
                call_id="prefetch",
                operation="query",
                success=True,
                result={"task_status": "running"},
            )
        )
    runtime.advance(3, True)
    runtime.delivery_started("r1", 3.1)
    runtime.observe(
        event(
            "native_operation_started",
            timestamp=3.2,
            call_id="fresh",
            operation="query",
        )
    )
    assert runtime.operations["prefetch"]["request_id"] is None
    assert runtime.operations["fresh"]["request_id"] == "r1"


def test_generated_text_can_score_when_played_audio_transcript_is_missing():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.add_played_audio(6.25, 6.5)
    runtime.observe(
        event(
            "native_operation_started", timestamp=5, call_id="query", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=6,
            call_id="query",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    runtime.add_generated_text("The task is still running.", 6.5, 7, response_id="r")
    summary = runtime.summary(8)
    assert (
        summary["reply_windows"]["r1"]["generated_text"] == "The task is still running."
    )
    assert summary["reply_windows"]["r1"]["reply_source"] == "generated_text"
    assert summary["reply_windows"]["r1"]["audio_transcript_status"] == "missing"
    result = grade(
        summary,
        CommunicateScoring.model_validate({}),
        reply_judge=lambda text, fact: {"met": text == "The task is still running."},
    )
    assert result["evaluation_log"]["reply"]["passed"] == 1.0
    assert result["completion_score"] == 1.0


def test_previous_reply_overlap_is_audio_diagnostic_only():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.add_reply("previous turn", 2.9, 3.2)
    summary = runtime.summary(5)
    assert summary["issues"] == []
    assert any(
        item["reason"] == "boundary_overlap_previous"
        for item in summary["audio_diagnostics"]
    )
    assert replay(summary)["audio_diagnostics"] == summary["audio_diagnostics"]


def test_v2_without_generated_reply_is_reply_unavailable():
    runtime = BackendTaskRuntime(config())
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.observe(
        event(
            "native_operation_started", timestamp=5, call_id="query", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=6,
            call_id="query",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    result = grade(runtime.summary(8), CommunicateScoring.model_validate({}))
    assert result["status"] == "reply_unavailable"
    assert result["completion_score"] is None


def test_declared_identity_lookup_does_not_count_as_a_duplicate_target_query():
    runtime = BackendTaskRuntime(config(allow_status_list_lookup=True))
    runtime.activate_stage(0, 0)
    runtime.observe(event("task_created", timestamp=1))
    runtime.advance(2, True)
    runtime.delivery_started("r1", 3)
    runtime.delivery_completed("r1", 4)
    runtime.observe(
        event(
            "native_operation_started",
            timestamp=5,
            call_id="list",
            operation="query",
            arguments={"list_all": True},
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=6,
            call_id="list",
            operation="query",
            success=True,
            result={
                "status": "ok",
                "tasks": [{"task_id": "native", "status": "running"}],
            },
        )
    )
    runtime.observe(
        event(
            "native_operation_started", timestamp=7, call_id="query", operation="query"
        )
    )
    runtime.observe(
        event(
            "native_operation_completed",
            timestamp=8,
            call_id="query",
            operation="query",
            success=True,
            result={"task_status": "running"},
        )
    )
    runtime.add_reply("Still running.", 8, 9)
    runtime.add_generated_text("Still running.", 8, 9, response_id="reply")
    verdict = grade(
        runtime.summary(10),
        CommunicateScoring.model_validate({}),
        reply_judge=lambda *_: {"met": True},
    )
    assert verdict["completion_score"] == 1
    assert (
        verdict["evaluation_log"]["native_result"]["checks"][0][
            "auxiliary_lookup_count"
        ]
        == 1
    )

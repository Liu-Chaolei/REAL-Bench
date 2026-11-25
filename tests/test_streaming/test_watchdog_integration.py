"""Exercise real watchdog checkpoints without providers, Docker or model APIs."""

import json
import time
from types import SimpleNamespace

import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.scenario import (
    ScenarioAction,
    ScenarioRecord,
    ScenarioSpec,
    ScenarioStage,
)
from tau2.data_model.simulation import TerminationReason
from tau2.data_model.tasks import Task, UserScenario
from tau2.data_model.watchdog import WatchdogConfig
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase
from tau2.orchestrator import watchdog
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator


@pytest.fixture
def make_orchestrator(monkeypatch):
    def unexpected_model(**kwargs):
        raise AssertionError("A live model must not be used in watchdog tests")

    monkeypatch.setattr(watchdog, "generate", unexpected_model)

    def make(**kwargs):
        agent = SimpleNamespace(
            get_init_state=lambda **kwargs: None,
            get_next_chunk=lambda *args, **kwargs: (
                AssistantMessage(role="assistant", content=None, contains_speech=False),
                None,
            ),
            is_stop=lambda message: False,
            stop=lambda *args, **kwargs: None,
        )
        user = SimpleNamespace(
            get_init_state=lambda: None,
            get_next_chunk=lambda *args, **kwargs: (
                UserMessage(role="user", content=None, contains_speech=False),
                None,
            ),
            stop=lambda *args, **kwargs: None,
        )
        settings = {
            "max_steps": None,
            "watchdog_config": WatchdogConfig(
                enabled=True,
                initial_check_ticks=2,
                check_interval_ticks=2,
                max_total_ticks=8,
            ),
        }
        settings.update(kwargs)
        return FullDuplexOrchestrator(
            "mock",
            agent,
            user,
            Environment("mock", "Fixture policy", ToolKitBase(DB())),
            Task(id="watchdog", user_scenario=UserScenario(instructions="Wait.")),
            **settings,
        )

    return make


@pytest.mark.parametrize(
    "first_decision", ["continue", "wait_one_more_checkpoint", "uncertain"]
)
@pytest.mark.parametrize("cap", [8, None])
def test_running_checkpoint_can_continue_then_finalize(
    monkeypatch, make_orchestrator, first_decision, cap
):
    contexts = []

    def judge(**kwargs):
        contexts.append(json.loads(kwargs["messages"][1].content.split("\n", 1)[1]))
        decision = first_decision if len(contexts) == 1 else "stop_and_evaluate"
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                dict(
                    decision=decision,
                    state="unknown",
                    confidence=1,
                    reason="Offline checkpoint",
                    evidence=[],
                )
            ),
        )

    monkeypatch.setattr(watchdog, "generate", judge)
    orchestrator = make_orchestrator(
        watchdog_config=WatchdogConfig(
            enabled=True,
            initial_check_ticks=2,
            check_interval_ticks=2,
            max_total_ticks=cap,
        )
    )
    result = orchestrator.run()

    assert len(contexts) == 2
    assert all(c["pending_operations"]["termination_reason"] is None for c in contexts)
    assert result.termination_reason == TerminationReason.WATCHDOG_STALL
    assert result.ticks
    assert [row["tick"] for row in result.info["watchdog"]["checkpoints"]] == [2, 4]
    assert result.info["watchdog"]["checkpoints"][0]["decision"] == first_decision


@pytest.mark.parametrize(
    "cause", ["user_stop", "agent_stop", "max_steps", "errors", "timeout"]
)
def test_existing_termination_is_not_overridden_by_watchdog(make_orchestrator, cause):
    orchestrator = make_orchestrator()
    orchestrator.step_count = 2
    calls = []
    orchestrator.watchdog.judge = lambda context: calls.append(context)
    reasons = {
        "user_stop": TerminationReason.USER_STOP,
        "agent_stop": TerminationReason.AGENT_STOP,
        "max_steps": TerminationReason.MAX_STEPS,
        "errors": TerminationReason.TOO_MANY_ERRORS,
        "timeout": TerminationReason.TIMEOUT,
    }
    if cause in {"user_stop", "agent_stop"}:
        orchestrator.done = True
        orchestrator.termination_reason = reasons[cause]
    elif cause == "max_steps":
        orchestrator.max_steps = 2
    elif cause == "errors":
        orchestrator.num_errors = orchestrator.max_errors
    else:
        orchestrator.timeout = 1
        orchestrator._run_start_perf = time.perf_counter() - 2

    orchestrator._check_termination()

    assert orchestrator.done
    assert orchestrator.termination_reason == reasons[cause]
    assert calls == []


@pytest.mark.parametrize("failure_at", ["context", "judge"])
def test_checkpoint_failure_preserves_run_and_schedules_next_check(
    monkeypatch, make_orchestrator, failure_at
):
    orchestrator = make_orchestrator()
    orchestrator.step_count = 2

    def broken(*args):
        raise ValueError("Offline failure")

    if failure_at == "context":
        monkeypatch.setattr(orchestrator, "_watchdog_context", broken)
    else:
        monkeypatch.setattr(orchestrator.watchdog, "judge", broken)

    orchestrator._check_termination()

    assert not orchestrator.done
    assert orchestrator.termination_reason is None
    assert orchestrator._watchdog_next_check == 4
    assert orchestrator.watchdog.checkpoints[0]["decision"] == "uncertain"
    assert "ValueError" in orchestrator.watchdog.checkpoints[0]["reason"]


def test_cap_and_heartbeat_stall_stop_without_model_call(make_orchestrator):
    for cause in ("cap", "heartbeat"):
        orchestrator = make_orchestrator()
        orchestrator.step_count = 8 if cause == "cap" else 2
        orchestrator._watchdog_stall_requested = cause == "heartbeat"
        calls = []
        orchestrator.watchdog.judge = lambda context: calls.append(context)
        orchestrator._check_termination()
        assert orchestrator.done
        assert orchestrator.termination_reason == TerminationReason.WATCHDOG_STALL
        assert calls == []


@pytest.mark.parametrize("config", [None, WatchdogConfig(enabled=False)])
def test_disabled_watchdog_keeps_original_max_steps(make_orchestrator, config):
    orchestrator = make_orchestrator(max_steps=3, watchdog_config=config)
    result = orchestrator.run()
    assert result.termination_reason == TerminationReason.MAX_STEPS
    assert "watchdog" not in result.info


def test_context_uses_current_stage_and_authoritative_runtime(make_orchestrator):
    orchestrator = make_orchestrator()
    orchestrator.task.scenario = ScenarioSpec(
        source_id="fixture",
        source_sha256="0" * 64,
        snapshot="0" * 64,
        category="fixture",
        start_time=1000,
        events=[
            ScenarioAction(
                id=f"u{i}",
                kind="user",
                tool="user",
                arguments={"content": f"Request {i}"},
            )
            for i in range(2)
        ],
        stages=[
            ScenarioStage(
                stage=i, request=f"Request {i}", event_ids=[f"u{i}"], criteria={}
            )
            for i in range(2)
        ],
    )
    orchestrator.scenario_stages = SimpleNamespace(
        current=1, transition_pending=True, awaiting_delivery=True
    )
    orchestrator.environment.scenario_runtime = SimpleNamespace(
        time=lambda: 1234.0,
        records=[
            ScenarioRecord(
                sequence=0,
                time=1200,
                kind="tool",
                name="lookup",
                call_id="call",
                result="found",
            )
        ],
    )
    payload = json.loads(orchestrator._watchdog_context().split("\n", 1)[1])
    assert payload["task"] == "Request 1"
    assert payload["environment"]["current_time_epoch"] == 1234
    assert payload["environment"]["stage"]["current_stage"] == 1
    assert payload["environment"]["stage"]["transition_pending"]
    assert payload["environment"]["stage"]["awaiting_delivery"]
    assert payload["recent_tool_calls"][0]["tool"] == "lookup"
    assert payload["recent_tool_calls"][0]["status"] == "success"


def test_reply_extension_cannot_bypass_watchdog_cap(make_orchestrator):
    orchestrator = make_orchestrator(max_steps=2)
    orchestrator.step_count = 8
    orchestrator.environment.scenario_runtime = SimpleNamespace(time=lambda: 1000)
    orchestrator.active_metric_manager = SimpleNamespace(
        should_extend_reply=lambda now: True, consume_reply_step=lambda now: None
    )
    calls = []
    orchestrator.watchdog.judge = lambda context: calls.append(context)
    orchestrator._check_termination()
    assert orchestrator.done
    assert orchestrator.termination_reason == TerminationReason.WATCHDOG_STALL
    assert not calls

import json
import time

from tau2.data_model.message import AssistantMessage, Tick, ToolCall, UserMessage
from tau2.data_model.semantic_communication import SemanticCommunicateSettings
from tau2.data_model.watchdog import WatchdogConfig
from tau2.orchestrator import watchdog


def test_watchdog_judge_parses_structured_reply(monkeypatch):
    def fake_generate(**kwargs):
        return AssistantMessage(
            role="assistant",
            content=(
                '{"decision":"stop_and_evaluate",'
                '"state":"backend_pending",'
                '"confidence":0.95,"reason":"request has no result",'
                '"evidence":["pending tool"]}'
            ),
        )

    monkeypatch.setattr(watchdog, "generate", fake_generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, judge_timeout_seconds=1),
        SemanticCommunicateSettings(model="test"),
    )

    verdict = controller.judge("pending tool context")

    assert verdict.decision == "stop_and_evaluate"
    assert verdict.state == "backend_pending"


def test_watchdog_normalizes_common_model_variants(monkeypatch):
    def fake_generate(**kwargs):
        return AssistantMessage(
            role="assistant",
            content=(
                '{"decision":"continue","state":"in_progress",'
                '"confidence":0.9,"reason":"still progressing",'
                '"evidence":{"tick":600,"phase":"tick_complete"}}'
            ),
        )

    monkeypatch.setattr(watchdog, "generate", fake_generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, judge_timeout_seconds=1),
        SemanticCommunicateSettings(model="test"),
    )

    verdict = controller.judge("progress context")

    assert verdict.decision == "continue"
    assert verdict.state == "progressing"
    assert verdict.evidence == ["tick=600", "phase=tick_complete"]


def test_watchdog_judge_timeout_is_conservative(monkeypatch):
    def slow_generate(**kwargs):
        time.sleep(0.2)
        return AssistantMessage(
            role="assistant",
            content='{"decision":"stop_and_evaluate","state":"unknown",'
            '"confidence":1,"reason":"late","evidence":[]}',
        )

    monkeypatch.setattr(watchdog, "generate", slow_generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, judge_timeout_seconds=0.01),
        SemanticCommunicateSettings(model="test"),
    )

    verdict = controller.judge("context")

    assert verdict.decision == "uncertain"
    assert "timed out" in verdict.reason


def test_compact_context_collapses_ticks_and_keeps_partial_user_turn():
    agent_start = AssistantMessage(
        role="assistant",
        content="The answer is Basel.",
        contains_speech=True,
        utterance_ids=["agent-1"],
    )
    agent_settled = AssistantMessage(
        role="assistant",
        content="The answer is Basel.",
        contains_speech=False,
    )
    user_first = UserMessage(
        role="user",
        content="Hold",
        contains_speech=True,
        is_final_chunk=False,
        utterance_ids=["user-1"],
    )
    user_second = UserMessage(
        role="user",
        content=" on",
        contains_speech=True,
        is_final_chunk=False,
        utterance_ids=["user-1"],
    )
    calls = [
        ToolCall(
            id="call-1",
            name="Calendar__search_events",
            arguments={"query": "Dinner with Family"},
        )
    ]
    ticks = [
        Tick(
            tick_id=1,
            timestamp="2026-01-01T00:00:00Z",
            agent_chunk=agent_start,
            user_chunk=UserMessage(role="user", content="", contains_speech=False),
            agent_tool_calls=calls,
        ),
        Tick(
            tick_id=2,
            timestamp="2026-01-01T00:00:01Z",
            agent_chunk=agent_settled,
            user_chunk=user_first,
        ),
        Tick(
            tick_id=3,
            timestamp="2026-01-01T00:00:02Z",
            agent_chunk=agent_settled,
            user_chunk=user_second,
        ),
    ]
    context = watchdog.compact_watchdog_context(
        task_text="Find the city.",
        ticks=ticks,
        environment_trace=[],
        pending={
            "phase": "tick_complete",
            "heartbeat_age_seconds": 0.1,
            "heartbeat_timeout_seconds": 180,
            "heartbeat_stale": False,
            "user_speech_active": True,
            "user_speech_final": False,
        },
        current_time=1767225602,
    )
    payload = json.loads(context.split("\n", 1)[1])

    assert payload["heartbeat"]["alive"] is True
    assert payload["conversation"]["active_turn"]["text"] == "Hold on"
    assert payload["conversation"]["active_turn"]["is_final"] is False
    assert len(payload["conversation"]["completed_turns"]) == 1
    assert (
        payload["conversation"]["completed_turns"][0]["text"] == "The answer is Basel."
    )
    assert len(payload["recent_tool_calls"]) == 1
    assert "RECENT TICKS" not in context

import json
import threading
import time

import pytest

from tau2.data_model.message import (
    AssistantMessage,
    Tick,
    ToolCall,
    ToolMessage,
    UserMessage,
)
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


@pytest.mark.parametrize("state", ["task_completed", "completed", "complete"])
def test_completion_verdict_is_rejected_and_keeps_run_alive(monkeypatch, state):
    def fake_generate(**kwargs):
        schema = kwargs["response_model"].model_json_schema()
        assert state not in schema["properties"]["state"]["enum"]
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                dict(
                    decision="stop_and_evaluate",
                    state=state,
                    confidence=0.9,
                    reason="All requested cancellations and notifications completed.",
                    evidence=["No pending tool results", "No active speech"],
                )
            ),
        )

    monkeypatch.setattr(watchdog, "generate", fake_generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, judge_timeout_seconds=1),
        SemanticCommunicateSettings(model="test"),
    )
    context = watchdog.compact_watchdog_context(
        task_text="Cancel morning Social events and notify the attendees.",
        ticks=[],
        environment_trace=[],
        pending={},
        stage={"current_stage": 0, "total_stages": 3},
    )

    verdict = controller.judge(context)

    assert verdict.decision == "uncertain"
    assert verdict.state == "unknown"
    assert "ValidationError" in verdict.reason


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


def test_watchdog_resolves_judge_connection_arguments(monkeypatch):
    seen = []

    def arguments(self):
        return {"api_key": "offline-credential", "api_base": "https://offline.invalid"}

    def generate(**kwargs):
        seen.append(kwargs)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                dict(
                    decision="continue",
                    state="progressing",
                    confidence=1,
                    reason="Offline",
                    evidence=[],
                )
            ),
        )

    monkeypatch.setattr(SemanticCommunicateSettings, "arguments", arguments)
    monkeypatch.setattr(watchdog, "generate", generate)
    controller = watchdog.WatchdogController(WatchdogConfig(enabled=True))
    assert controller.judge("context").decision == "continue"
    assert seen[0]["api_key"] == "offline-credential"
    assert seen[0]["api_base"] == "https://offline.invalid"


def test_own_bounded_judge_wait_keeps_heartbeat_alive(monkeypatch):
    def generate(**kwargs):
        time.sleep(0.15)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                dict(
                    decision="continue",
                    state="progressing",
                    confidence=1,
                    reason="Offline",
                    evidence=[],
                )
            ),
        )

    monkeypatch.setattr(watchdog, "generate", generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(
            enabled=True, heartbeat_timeout_seconds=0.04, judge_timeout_seconds=1
        )
    )
    stale = threading.Event()
    controller.start(stale.set)
    try:
        assert controller.judge("context").decision == "continue"
        assert not stale.is_set()
        assert not controller.stale
    finally:
        controller.stop()


def test_real_heartbeat_stall_is_sticky_and_reported_once():
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, heartbeat_timeout_seconds=0.03)
    )
    stale = threading.Event()
    calls = []

    def on_stale():
        calls.append(True)
        stale.set()

    controller.start(on_stale)
    try:
        assert stale.wait(1)
        controller.heartbeat(5, "returned_after_stall")
        assert controller.stale
        assert calls == [True]
    finally:
        controller.stop()


def test_pending_judge_does_not_start_parallel_calls_or_reuse_late_stop(monkeypatch):
    release = threading.Event()
    calls = []

    def generate(**kwargs):
        calls.append(kwargs["messages"][1].content)
        if len(calls) == 1:
            release.wait(1)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                dict(
                    decision="stop_and_evaluate" if len(calls) == 1 else "continue",
                    state="unknown",
                    confidence=1,
                    reason="Offline",
                    evidence=[],
                )
            ),
        )

    monkeypatch.setattr(watchdog, "generate", generate)
    controller = watchdog.WatchdogController(
        WatchdogConfig(enabled=True, judge_timeout_seconds=0.02)
    )
    try:
        assert controller.judge("old").decision == "uncertain"
        assert controller.judge("while_pending").decision == "uncertain"
        assert calls == ["old"]
        release.set()
        controller._judge_thread.join(timeout=1)
        assert controller.judge("new").decision == "continue"
        assert calls == ["old", "new"]
    finally:
        release.set()
        controller.stop()


def test_context_reads_confirmed_live_fragments_once_instead_of_cumulative_content():
    ticks = []
    for index, (content, fragments) in enumerate(
        [
            (
                "First sentence.",
                [{"event_id": "f1", "speaker": "agent", "text": "First sentence."}],
            ),
            (
                "First sentence. Second sentence.",
                [{"event_id": "f2", "speaker": "agent", "text": "Second sentence."}],
            ),
            ("First sentence. Second sentence.", []),
            (
                "First sentence.",
                [{"event_id": "f1", "speaker": "agent", "text": "First sentence."}],
            ),
        ]
    ):
        ticks.append(
            Tick(
                tick_id=index,
                timestamp=str(index),
                agent_chunk=AssistantMessage(
                    role="assistant",
                    content=content,
                    contains_speech=False,
                    raw_data={
                        "live_transcript": {"session_id": "s", "fragments": fragments}
                    },
                ),
            )
        )
    payload = json.loads(
        watchdog.compact_watchdog_context(
            task_text="Listen.",
            ticks=ticks,
            environment_trace=[],
            pending={},
        ).split("\n", 1)[1]
    )
    rows = payload["conversation"]["completed_turns"]
    assert [row["text"] for row in rows] == ["First sentence.", "Second sentence."]
    assert all(row["time_basis"] == "asr_release_host_wall_clock" for row in rows)


def test_context_does_not_keep_interrupted_user_speech_active():
    ticks = [
        Tick(
            tick_id=0,
            timestamp="0",
            user_chunk=UserMessage(
                role="user",
                content="Wait",
                contains_speech=True,
                is_final_chunk=False,
                utterance_ids=["u1"],
            ),
        ),
        Tick(
            tick_id=1,
            timestamp="1",
            user_chunk=UserMessage(
                role="user",
                content="",
                contains_speech=False,
            ),
        ),
    ]
    payload = json.loads(
        watchdog.compact_watchdog_context(
            task_text="Listen.",
            ticks=ticks,
            environment_trace=[],
            pending={},
        ).split("\n", 1)[1]
    )
    assert payload["conversation"]["active_turn"] is None
    assert payload["conversation"]["completed_turns"][0]["status"] == "interrupted"


def test_tool_summary_keeps_environment_clock_and_recent_twenty():
    trace = [
        dict(
            kind="tool",
            name="lookup",
            call_id=f"c{i}",
            time=1000 + i,
            started_time=1000 + i - 0.5,
            result="done",
        )
        for i in range(25)
    ]
    ticks = [
        Tick(
            tick_id=0,
            timestamp="2026-01-01T00:00:00",
            agent_tool_calls=[ToolCall(id="c24", name="lookup", arguments={})],
            agent_tool_results=[ToolMessage(id="c24", role="tool", content="done")],
        )
    ]
    payload = json.loads(
        watchdog.compact_watchdog_context(
            task_text="Lookup.",
            ticks=ticks,
            environment_trace=trace,
            pending={},
        ).split("\n", 1)[1]
    )
    rows = payload["recent_tool_calls"]
    assert len(rows) == 20
    assert rows[-1]["time_basis"] == "environment"
    assert rows[-1]["end_time"] == "1970-01-01T00:17:04+00:00"

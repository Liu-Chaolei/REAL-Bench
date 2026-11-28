"""Actual chunk assembly, ASR release and end-of-call evidence contracts."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage, UserMessage
from tau2.data_model.simulation import SimulationRun
from tau2.data_model.tasks import Task, UserScenario
from tau2.data_model.transcript import LiveTranscriptTick
from tau2.diagnostics.collect import EvidenceRecorder, finalize_evidence
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.state.capture import CAPTURE_VERSION
from tau2.diagnostics.state.events import recorded_events
from tau2.diagnostics.state.sidecar import StateShadowWorker
from tau2.diagnostics.suite import evaluate_evidence, load_evidence


@pytest.fixture
def recorder(tmp_path):
    environment = SimpleNamespace(
        get_domain_name=lambda: "mock",
        tools=SimpleNamespace(
            tools={},
            db=SimpleNamespace(model_dump=lambda **_: {}),
        ),
    )
    task = Task(id="speech", user_scenario=UserScenario(instructions="Private script"))
    value = EvidenceRecorder(environment, task, tmp_path / "evidence")
    yield value
    value.close()


def feed(recorder, user=None, agent=None):
    index = len(recorder.media_ticks)
    recorder.media(
        user,
        agent or AssistantMessage(role="assistant", contains_speech=False),
        100 + index * 0.2,
        100 + (index + 1) * 0.2,
        0.2,
    )


def speech(text, *, final=False, ident="request", **kwargs):
    return UserMessage(
        role="user",
        content=text,
        utterance_ids=[ident],
        is_final_chunk=final,
        contains_speech=True,
        **kwargs,
    )


def live(*fragments):
    return LiveTranscriptTick(
        session_id="asr",
        origin="recorded_audio",
        alignment="played_audio",
        fragments=[dict(speaker="agent", **f) for f in fragments],
    )


def test_state_gets_full_caller_utterance_and_never_just_final_letters(recorder):
    received = []
    recorder.set_state_event_sink(received.append)
    feed(recorder, speech("Give me the first "))
    feed(recorder, speech("name of the contac"))
    assert received == []
    feed(recorder, speech("t.", final=True))
    assert len(received) == 1
    event = received[0]
    assert event["content"] == "Give me the first name of the contact."
    assert event["metadata"]["incomplete"] is False
    assert event["timestamp"] == pytest.approx(100.6)
    assert event["metadata"]["interval"] == pytest.approx([100, 100.6])


def test_empty_final_chunk_closes_only_the_matching_utterance(recorder):
    feed(recorder, speech("Keep "))
    feed(recorder, UserMessage(role="user", contains_speech=False))
    assert recorder.state_events == []
    feed(recorder, speech("going"))
    feed(recorder, speech(None, final=True))
    assert [r["content"] for r in recorder.state_events] == ["Keep going"]


def test_delivered_silent_gold_prefix_does_not_mark_complete_speech_partial(recorder):
    template = "<chunk id=0></chunk><chunk id=1>Find Kaidën.</chunk>"
    for active, text, final in [("0", "", False), ("1", "Find Kaidën.", True)]:
        feed(
            recorder,
            UserMessage(
                role="user",
                content=text,
                contains_speech=bool(text),
                utterance_ids=["r"],
                is_final_chunk=final,
                audio_script_gold=f'<message uuid="r" active="{active}">{template}</message>',
            ),
        )
    assert recorder.state_events[0]["content"] == "Find Kaidën."
    assert recorder.state_events[0]["metadata"]["incomplete"] is False


@pytest.mark.parametrize("final_asr", [False, True])
def test_orchestrator_records_only_chunks_actually_consumed(recorder, final_asr):
    from tau2.environment.db import DB
    from tau2.environment.environment import Environment
    from tau2.environment.toolkit import ToolKitBase
    from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator

    chunks = iter(
        [
            speech("Find the "),
            speech("contact.", final=True),
            speech("NOT TRANSPORTED", final=True, ident="unused"),
        ]
    )
    caller = SimpleNamespace(
        get_init_state=lambda: None,
        get_next_chunk=lambda *a, **k: (next(chunks), None),
        stop=lambda *a, **k: None,
    )
    agent = SimpleNamespace(
        get_init_state=lambda **k: None,
        get_next_chunk=lambda *a, **k: (
            AssistantMessage(role="assistant", content=None, contains_speech=False),
            None,
        ),
        is_stop=lambda message: False,
        stop=lambda *a, **k: None,
        get_final_live_transcript=lambda: (
            live(dict(event_id="late", text="Late answer.")) if final_asr else None,
            None,
        ),
    )
    environment = Environment("mock", "Policy", ToolKitBase(DB()))
    environment.diagnostic_recorder = recorder
    orchestrator = FullDuplexOrchestrator(
        "mock",
        agent,
        caller,
        environment,
        recorder.task,
        max_steps=2,
    )
    result = orchestrator.run()
    assert result.ticks
    users = [e["content"] for e in recorder.state_events if e["kind"] == "user_turn"]
    assert users == ["Find the contact."]
    if final_asr:
        events = [e for e in recorder.state_events if e["event_id"].startswith("live:")]
        assert [e["content"] for e in events] == ["Late answer."]
        assert (
            recorder.media_ticks[-1].agent_chunk.raw_data["live_transcript"][
                "fragments"
            ][0]["event_id"]
            == "late"
        )


@pytest.mark.parametrize("end", ["call_end", "new_utterance", "stop"])
def test_interrupted_gold_does_not_publish_the_unplayed_script(recorder, end):
    gold = (
        '<message uuid="r" active="0"><chunk id=0>Read </chunk>'
        "<chunk id=1>PRIVATE UNPLAYED SUFFIX</chunk></message>"
    )
    feed(recorder, speech("Read ", audio_script_gold=gold))
    if end == "call_end":
        recorder.finish_dialogue()
    elif end == "new_utterance":
        feed(recorder, speech("Cancel that.", ident="next", final=True))
    else:
        feed(recorder, speech("###STOP###", final=True))
    event = recorder.state_events[0]
    assert event["content"] == "Read "
    assert event["metadata"]["incomplete"] is True
    assert "PRIVATE" not in json.dumps(recorder.state_events)


def test_agent_text_chunks_are_assembled_without_future_gold(recorder):
    for i, text in enumerate(["I found ", "the contact."]):
        feed(
            recorder,
            agent=AssistantMessage(
                role="assistant",
                content=text,
                utterance_ids=["reply"],
                is_final_chunk=i == 1,
            ),
        )
    assert [e["content"] for e in recorder.state_events] == ["I found the contact."]


def test_confirmed_live_fragments_ignore_snapshots_and_work_on_silent_ticks(recorder):
    first = live(dict(event_id="one", text="Found Kaidën.", start_ms=0, end_ms=100))
    second = live(
        dict(event_id="one", text="Found Kaidën.", start_ms=0, end_ms=100),
        dict(event_id="two", text="Found Kaidën.", start_ms=200, end_ms=300),
    )
    for value in [first, first, second]:
        feed(
            recorder,
            agent=AssistantMessage(
                role="assistant",
                content="CUMULATIVE SNAPSHOT MUST NOT BE USED",
                contains_speech=False,
                raw_data={"live_transcript": value.model_dump()},
            ),
        )
    assert [e["content"] for e in recorder.state_events] == [
        "Found Kaidën.",
        "Found Kaidën.",
    ]
    assert len({e["event_id"] for e in recorder.state_events}) == 2


def test_shutdown_asr_is_published_once_and_partial_caller_is_marked(recorder):
    first = dict(event_id="one", text="Found ")
    feed(
        recorder,
        speech("Could you "),
        AssistantMessage(
            role="assistant",
            contains_speech=False,
            raw_data={"live_transcript": live(first).model_dump()},
        ),
    )
    final = live(first, dict(event_id="two", text="Kaidën."))
    recorder.finish_dialogue(final)
    recorder.finish_dialogue(final)
    recorder.close()
    assert [e["content"] for e in recorder.state_events] == [
        "Found ",
        "Could you ",
        "Kaidën.",
    ]
    assert recorder.state_events[1]["metadata"]["incomplete"] is True


def test_background_metadata_never_becomes_a_formal_user_goal(recorder):
    feed(
        recorder,
        speech(
            "Find the physics contact.",
            final=True,
            raw_data={
                "background_text": "Could you dim the lamp?",
                "saasr_foreground": {},
            },
        ),
    )
    assert [e["content"] for e in recorder.state_events] == [
        "Find the physics contact."
    ]


def test_mixed_events_are_sealed_once_with_full_dialogue(recorder, monkeypatch):
    monkeypatch.setattr(
        "tau2.diagnostics.collect.collect_audio",
        lambda *args: {"activity": {"origin": 100}, "status": "collected"},
    )
    feed(recorder, speech("Find the "))
    feed(recorder, speech("physics contact.", final=True))
    call = ToolCall(id="lookup", name="Contacts__find", arguments={})
    token = recorder.before(call, {})
    recorder.after(
        token,
        call,
        {},
        ToolMessage(
            role="tool", id="lookup", requestor="assistant", content='{"name":"Kaidën"}'
        ),
    )
    recorder.tool_delivered("lookup")
    feed(recorder, agent=AssistantMessage(role="assistant", content="Kaidën."))
    original = deepcopy(recorder.state_events)
    simulation = SimulationRun(
        id="s",
        task_id=recorder.task.id,
        start_time="a",
        end_time="b",
        duration=1,
        termination_reason="user_stop",
        ticks=recorder.media_ticks,
    )
    orchestrator = SimpleNamespace(
        task=recorder.task,
        environment=recorder.environment,
        agent=SimpleNamespace(),
    )
    final = finalize_evidence(orchestrator, simulation, recorder, recorder.directory)
    saved = load_evidence(recorder.directory)
    assert saved == final
    assert [e["content"] for e in final.state_events if e["kind"] == "user_turn"] == [
        "Find the physics contact."
    ]
    assert [e["content"] for e in final.state_events if e["kind"] == "agent_turn"] == [
        "Kaidën."
    ]
    tools = [e for e in final.state_events if e["kind"].startswith("tool_")]
    assert len(tools) == 3
    assert next(e for e in tools if e["kind"] == "tool_delivered")["content"][
        "result"
    ] == {"name": "Kaidën"}
    assert recorder.state_events == original


def test_voice_event_normalization_does_not_dict_coerce_string_dialogue():
    rows = [
        dict(
            kind="user_turn",
            content="t.",
            event_id="old",
            role="user",
            visible_to=["frontend"],
            timestamp=101,
            metadata={},
        )
    ]
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="mock",
        audio={"activity": {"origin": 100}},
        user_inputs=[UserInput(id="full", text="The contact.", started=0, ended=1)],
    )
    result = recorded_events(SimpleNamespace(ticks=[object()]), evidence, rows)
    assert len(result) == 1 and result[0]["content"] == "The contact."


@pytest.mark.parametrize("audio", [{}, {"activity": {"origin": 100}}])
def test_captured_dialogue_keeps_its_clock_and_is_not_rebuilt_from_snapshots(audio):
    rows = [
        dict(
            event_id="caller",
            kind="user_turn",
            role="user",
            content="Find the contact.",
            visible_to=["frontend"],
            timestamp=101,
            metadata={"capture_version": CAPTURE_VERSION},
        ),
        dict(
            event_id="agent",
            kind="agent_turn",
            role="frontend",
            content="Kaidën.",
            visible_to=["frontend"],
            timestamp=102,
            metadata={"capture_version": CAPTURE_VERSION},
        ),
    ]
    original = deepcopy(rows)
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="mock",
        audio=audio,
        user_inputs=[
            UserInput(
                id="caller-snapshot", text="Find the contact.", started=0, ended=1
            ),
            UserInput(
                id="agent-snapshot",
                role="assistant_context",
                text="Kaidën.",
                started=1,
                ended=2,
            ),
        ],
    )

    result = recorded_events(SimpleNamespace(ticks=[object()]), evidence, rows)

    assert [e["event_id"] for e in result] == ["caller", "agent"]
    assert [e["content"] for e in result] == ["Find the contact.", "Kaidën."]
    assert [e["timestamp"] for e in result] == [101, 102]
    assert [e["sequence"] for e in result] == [0, 1]
    assert rows == original
    result[0]["metadata"]["incomplete"] = True
    assert rows == original


@pytest.mark.parametrize("current", [False, True])
def test_legacy_sidecar_scores_are_not_reused_after_capture_fix(
    tmp_path, monkeypatch, current
):
    sidecar = tmp_path / "state_sidecar"
    sidecar.mkdir()
    status = {"status": "complete"}
    if current:
        status["capture_version"] = CAPTURE_VERSION
    (sidecar / "status.json").write_text(json.dumps(status))
    (sidecar / "state_trace.json").write_text("{}")
    (sidecar / "state_interaction.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "marker": "online",
            }
        )
    )
    calls = []

    def offline(*args, **kwargs):
        calls.append(True)
        return {"status": "complete", "marker": "offline"}

    monkeypatch.setattr(
        "tau2.diagnostics.state.metrics.evaluate_state_interaction", offline
    )
    report = evaluate_evidence(
        Task(id="t", user_scenario=UserScenario(instructions="work")),
        DiagnosticEvidence(simulation_id="s", task_id="t", domain="mock"),
        tmp_path / "evidence",
        MetricsConfig(measures=["state_interaction"]),
    )["summary"]["measures"]["state_interaction"]
    assert report["marker"] == ("online" if current else "offline")
    assert bool(calls) is not current


def test_bad_capture_does_not_abort_media_or_claim_sidecar_complete(recorder, tmp_path):
    feed(
        recorder,
        agent=AssistantMessage(
            role="assistant",
            raw_data={"live_transcript": {"session_id": "bad", "alignment": "invalid"}},
        ),
    )
    assert recorder.errors[0]["kind"] == "state_dialogue_capture"
    worker = StateShadowWorker(
        recorder.task,
        recorder,
        MetricsConfig(
            measures=["state_interaction"], state_sidecar_stability_seconds=0
        ),
        tmp_path / "sidecar",
    )
    worker.start()
    worker.close_and_drain(1)
    status = json.loads((tmp_path / "sidecar/status.json").read_text())
    assert status["status"] == "partial"

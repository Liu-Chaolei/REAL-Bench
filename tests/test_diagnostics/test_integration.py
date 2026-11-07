"""Behavioral coverage of portable evidence, whole-call intent and native execution."""

import json
from types import SimpleNamespace

import pytest

from tau2.data_model.diagnostics import (
    BackendInput,
    DiagnosticEvidence,
    OperationEvidence,
    UserInput,
)
from tau2.data_model.message import ToolCall
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec, ScenarioStage
from tau2.data_model.tasks import Task, UserScenario
from tau2.diagnostics.collect import EvidenceRecorder
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.execution.gaia2 import Gaia2ExecutionAdapter
from tau2.diagnostics.execution.matching import match_operations
from tau2.diagnostics.intent.call import combined_sources, evaluate_call
from tau2.diagnostics.model import RequestBudget


def task():
    events = [
        ScenarioAction(
            id="u0", kind="user", tool="user", arguments={"content": "Save apartment a"}
        ),
        ScenarioAction(
            id="save",
            kind="reference",
            tool="RentAFlat__save_apartment",
            arguments={"apartment_id": "a"},
            dependencies=["u0"],
        ),
        ScenarioAction(
            id="u1",
            kind="user",
            tool="user",
            arguments={"content": "Remove apartment a"},
            dependencies=["save"],
        ),
        ScenarioAction(
            id="remove",
            kind="reference",
            tool="RentAFlat__remove_saved_apartment",
            arguments={"apartment_id": "a"},
            dependencies=["u1"],
        ),
    ]
    stages = [
        ScenarioStage(
            stage=0, request="Save apartment a", event_ids=["u0", "save"], criteria={}
        ),
        ScenarioStage(
            stage=1,
            request="Remove apartment a",
            event_ids=["u1", "remove"],
            criteria={},
        ),
    ]
    return Task(
        id="sequence",
        user_scenario=UserScenario(instructions="Save then remove"),
        scenario=ScenarioSpec(
            start_time=1000,
            source_id="s",
            source_sha256="0" * 64,
            snapshot="0" * 64,
            category="execution",
            events=events,
            stages=stages,
        ),
    )


def evidence():
    return DiagnosticEvidence(
        simulation_id="call",
        task_id="sequence",
        domain="gaia2",
        backend_status="complete",
        user_inputs=[
            UserInput(id="u0", text="Save apartment a", started=0, ended=1, stage=0),
            UserInput(id="u1", text="Remove apartment a", started=3, ended=4, stage=1),
        ],
        backend_inputs=[
            BackendInput(id="p0", content="Save apartment a", time=1, accepted=True),
            BackendInput(id="p1", content="Remove apartment a", time=4, accepted=True),
        ],
        stages={
            "stages": [
                {"stage": 0, "started": {"time": 1000}, "completed": {"time": 1001}},
                {"stage": 1, "started": {"time": 1003}, "completed": {"time": 1004}},
                {"stage": 2, "started": None, "completed": None},
            ]
        },
        source={
            "termination_reason": "user_stop",
            "initial_frame": "empty",
            "final_frame": "empty",
        },
        frames={
            "empty": {"RentAFlat": {"saved_apartments": []}},
            "saved": {"RentAFlat": {"saved_apartments": ["a"]}},
        },
        operations=[
            OperationEvidence(
                call_id="c0",
                name="RentAFlat__save_apartment",
                app="RentAFlat",
                kind="WRITE",
                raw_arguments={"apartment_id": "a"},
                arguments={"apartment_id": "a"},
                started=1001,
                ended=1001.5,
                before_frame="empty",
                after_frame="saved",
            ),
            OperationEvidence(
                call_id="c1",
                name="RentAFlat__remove_saved_apartment",
                app="RentAFlat",
                kind="WRITE",
                raw_arguments={"apartment_id": "a"},
                arguments={"apartment_id": "a"},
                started=1004,
                ended=1004.5,
                before_frame="saved",
                after_frame="empty",
            ),
        ],
    )


def test_whole_call_sources_keep_two_delivered_requests_without_future_stage():
    a, b, reason = combined_sources(evidence())
    assert reason is None
    assert len(json.loads(a.text)) == len(json.loads(b.text)) == 2
    assert 'stage":2' not in a.text
    assert "saved_apartments" not in a.text + b.text


def test_partial_delivery_and_unconfirmed_backend_do_not_become_whole_text():
    e = evidence()
    e.user_inputs[0].unavailable_reason = "partial_delivery_text_unverified"
    assert combined_sources(e)[2] == "partial_delivery_text_unverified"
    e = evidence()
    e.backend_inputs[0].accepted = None
    assert combined_sources(e)[2] == "backend_delivery_unconfirmed"


@pytest.mark.asyncio
async def test_whole_call_intent_and_empty_prediction(monkeypatch, tmp_path):
    from tau2.diagnostics.intent import call

    prompts = []

    class Model:
        def __init__(self, config, role, *a):
            self.role = role

        async def complete(self, prompt, schema):
            prompts.append((self.role, prompt))
            if self.role in {"gt", "prediction"}:
                return json.dumps(
                    {"intent": {"goals": [{"id": "g", "action": "save_apartment"}]}}
                )
            return json.dumps({"judgments": []})

    monkeypatch.setattr(call, "TauJsonModel", Model)
    e = evidence()
    e.backend_inputs = []
    result = await evaluate_call(e, MetricsConfig(), tmp_path, RequestBudget(10))
    assert result["f1"] == 0 and result["recall"] == 0 and result["precision"] == 1
    assert [r for r, p in prompts] == ["gt"]
    assert "Save apartment a" in prompts[0][1] and "Remove apartment a" in prompts[0][1]


def test_intermediate_effects_survive_later_legitimate_deletion():
    tools = {
        "RentAFlat__save_apartment": {"write": True, "app": "RentAFlat"},
        "RentAFlat__remove_saved_apartment": {"write": True, "app": "RentAFlat"},
    }
    result = Gaia2ExecutionAdapter(tools).evaluate(task(), evidence(), None)
    assert result["total"]["score"] == 100
    assert result["dimensions"]["effects"]["score"] == 1
    assert result["dimensions"]["tools"]["score"] == 1


def test_missing_later_operation_lowers_execution_without_erasing_earlier_effect():
    tools = {
        "RentAFlat__save_apartment": {"write": True, "app": "RentAFlat"},
        "RentAFlat__remove_saved_apartment": {"write": True, "app": "RentAFlat"},
    }
    e = evidence()
    e.operations = e.operations[:1]
    result = Gaia2ExecutionAdapter(tools).evaluate(task(), e, None)
    assert result["dimensions"]["tools"]["recall"] == 0.5
    assert result["dimensions"]["effects"]["score"] == 0.5
    assert result["total"]["score"] < 100


def test_earlier_call_cannot_satisfy_unreleased_requirement():
    ref = [
        {
            "id": "r",
            "tool": "ApartmentListingApp__save_apartment",
            "args": {"apartment_id": "a"},
            "time": None,
        }
    ]
    actual = [
        {"id": "c", "tool": ref[0]["tool"], "args": {"apartment_id": "a"}, "time": 1}
    ]
    matching = match_operations(ref, actual, eligibility=lambda r, a: a["time"] > 2)
    assert matching["pairs"] == {} and matching["alternatives"] == {0: [None]}


def test_recorder_keeps_raw_parameters_and_real_effects(tmp_path):
    from tau2.domains.mock.environment import get_environment

    env = get_environment()
    recorder = EvidenceRecorder(env, task(), tmp_path)
    env.diagnostic_recorder = recorder
    call = ToolCall(
        id="bad", name="not_a_tool", arguments={"x": 1}, requestor="assistant"
    )
    result = env.get_response(call)
    recorder.close()
    assert result.error
    assert recorder.operations[0].raw_arguments == {"x": 1}
    assert recorder.operations[0].error
    assert recorder.initial_frame in recorder.frames
    assert not recorder.errors


def test_manifest_tampering_is_rejected(tmp_path):
    from tau2.diagnostics.intent.io import write_json
    from tau2.diagnostics.suite import load_evidence

    write_json(tmp_path / "evidence.json", evidence().model_dump(mode="json"))
    write_json(tmp_path / "manifest.json", {"files": {"evidence.json": "0" * 64}})
    with pytest.raises(ValueError, match="hash mismatch"):
        load_evidence(tmp_path)


def test_non_backend_agent_is_not_applicable(tmp_path):
    from tau2.diagnostics.suite import evaluate_evidence

    e = evidence()
    e.backend_status = "not_applicable"
    r = evaluate_evidence(
        task(), e, tmp_path / "evidence", MetricsConfig(measures=["intent"])
    )
    assert r["summary"]["measures"]["intent"]["status"] == "not_applicable"
    assert r["summary"]["requests_this_run"] == 0


def test_delivered_voice_reads_markup_and_excludes_unplayed_source():
    from tau2.data_model.message import Tick, UserMessage
    from tau2.diagnostics.collect import delivered_inputs

    chunk = UserMessage(
        role="user",
        content="Save a and delete b",
        contains_speech=True,
        is_final_chunk=True,
        utterance_ids=["u"],
        audio_script_gold='<message uuid="u" active="0"><chunk id=0>Save a</chunk><chunk id=1> and delete b</chunk></message>',
    )
    sim = SimpleNamespace(
        ticks=[
            Tick(
                tick_id=0, timestamp="now", tick_duration_seconds=0.2, user_chunk=chunk
            )
        ],
        info={},
        messages=None,
    )
    inputs = delivered_inputs(sim, task())
    assert len(inputs) == 1
    assert inputs[0].text == "Save a"
    assert "delete b" not in inputs[0].text
    assert inputs[0].unavailable_reason is None


def test_saved_evidence_survives_original_evaluator_failure(monkeypatch, tmp_path):
    pytest.importorskip("pyaudio")
    from tau2.data_model.simulation import SimulationRun, TerminationReason
    from tau2.domains.mock.environment import get_environment
    from tau2.runner import simulation as runner

    env = get_environment()
    sim = SimulationRun(
        id="s",
        task_id="sequence",
        start_time="now",
        end_time="now",
        duration=0,
        termination_reason=TerminationReason.USER_STOP,
        messages=[],
    )

    def run():
        env.close()
        return sim

    orchestrator = SimpleNamespace(
        call_context=None,
        environment=env,
        task=task(),
        agent=SimpleNamespace(),
        user=SimpleNamespace(),
        run=run,
    )

    def fail(**kwargs):
        raise ValueError("original evaluator failure")

    monkeypatch.setattr(runner, "evaluate_simulation", fail)
    result = runner.run_simulation(
        orchestrator,
        metrics_config=MetricsConfig(measures=["intent", "voice"]).model_dump(),
        diagnostics_dir=tmp_path / "diagnostics",
    )
    assert result.reward_info is None and result.evaluation_status == "failed"
    assert (tmp_path / "diagnostics/evidence/manifest.json").exists()
    assert (
        result.diagnostics_ref["summary"]["measures"]["intent"]["status"]
        == "not_applicable"
    )
    assert (
        result.diagnostics_ref["summary"]["measures"]["voice"]["status"]
        == "not_applicable"
    )


def test_real_voice_wrapper_uses_played_waveforms(tmp_path):
    pytest.importorskip("pyaudio")
    import base64
    import math
    import struct

    from tau2.data_model.audio import AudioEncoding, AudioFormat
    from tau2.data_model.message import AssistantMessage, Tick, UserMessage
    from tau2.diagnostics.collect import collect_audio

    pytest.importorskip("soxr")
    pytest.importorskip("webrtcvad")
    from tau2.diagnostics.voice import evaluate_voice

    rate = 16000
    fmt = AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=rate)

    def pcm(speech):
        return (
            b"".join(
                struct.pack(
                    "<h",
                    int(
                        10000 * math.sin(2 * math.pi * 180 * i / rate)
                        + 3000 * math.sin(2 * math.pi * 360 * i / rate)
                    ),
                )
                for i in range(rate)
            )
            if speech
            else bytes(2 * rate)
        )

    ticks = []
    for i in range(5):
        ticks.append(
            Tick(
                tick_id=i,
                timestamp="now",
                tick_duration_seconds=1,
                user_chunk=UserMessage(
                    role="user",
                    content="Request" if i == 0 else None,
                    contains_speech=i == 0,
                    audio_content=base64.b64encode(pcm(i == 0)).decode(),
                    audio_format=fmt,
                ),
                agent_chunk=AssistantMessage(
                    role="assistant",
                    content="Answer" if i == 2 else None,
                    contains_speech=i == 2,
                    audio_content=base64.b64encode(pcm(i == 2)).decode(),
                    audio_format=fmt,
                ),
            )
        )
    sim = SimpleNamespace(ticks=ticks)
    e = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=[UserInput(id="u", text="Request", started=0, ended=1)],
    )
    e.audio = collect_audio(sim, tmp_path)
    result = evaluate_voice(e, tmp_path)
    assert result["metrics"]["response_rate"]["value"] == 1
    assert 0 < result["metrics"]["response_latency_mean"]["value"] < 2.1
    assert 1.9 < result["waveform_evidence"]["agent_intervals"][0][0] < 2.1
    assert result["metrics"]["audible_pretoolspeech_rate"]["value"] is None


def test_generic_streaming_agent_collects_consumed_requests_in_one_call(
    monkeypatch, tmp_path
):
    pytest.importorskip("pyaudio")
    from tau2.data_model.message import AssistantMessage, UserMessage
    from tau2.data_model.simulation import RewardInfo
    from tau2.diagnostics.suite import load_evidence
    from tau2.environment.db import DB
    from tau2.environment.environment import Environment
    from tau2.environment.scenario import ScenarioRuntime
    from tau2.environment.toolkit import ToolKitBase
    from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
    from tau2.runner import simulation as runner

    class Caller:
        def __init__(self):
            self.inputs = []

        def queue_scenario_input(self, key, text):
            self.inputs.append((key, text))

        def get_init_state(self):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if self.inputs:
                key, text = self.inputs.pop(0)
                return UserMessage(
                    role="user",
                    content=text,
                    contains_speech=True,
                    is_final_chunk=True,
                    raw_data={"scenario_input_id": key},
                ), state
            return UserMessage(
                role="user",
                content="###STOP###"
                if participant_chunk and participant_chunk.content == "Done"
                else None,
                contains_speech=False,
            ), state

        def stop(self, *a, **kw):
            pass

    class Agent:
        has_pending_output = False

        def get_init_state(self, **kw):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if participant_chunk and participant_chunk.content:
                return AssistantMessage(
                    role="assistant",
                    content="Done",
                    contains_speech=True,
                    is_final_chunk=True,
                ), state
            return AssistantMessage(
                role="assistant", content=None, contains_speech=False
            ), state

        def is_stop(self, message):
            return False

        def stop(self, *a, **kw):
            pass

    t = task()
    env = Environment("mock", "policy", tools=ToolKitBase(DB()))
    env.scenario_runtime = ScenarioRuntime(
        t.scenario, lambda *a: None, managed_user_events={"u1"}
    )
    orchestrator = FullDuplexOrchestrator(
        "mock", Agent(), Caller(), env, t, max_steps=30, tick_duration_seconds=0.001
    )
    monkeypatch.setattr(
        runner, "evaluate_simulation", lambda **kw: RewardInfo(reward=1)
    )
    result = runner.run_simulation(
        orchestrator,
        metrics_config={"measures": ["intent"]},
        diagnostics_dir=tmp_path / "diagnostics",
    )
    e = load_evidence(tmp_path / "diagnostics/evidence")
    assert [r.text for r in e.user_inputs if r.role == "user"] == [
        "Save apartment a",
        "Remove apartment a",
    ]
    assert [r.stage for r in e.user_inputs if r.role == "user"] == [0, 1]
    assert e.backend_status == "not_applicable"
    assert result.reward_info.reward == 1


def test_native_semantic_group_preserves_checker_prompts():
    pytest.importorskip("bs4")
    from tau2.diagnostics.execution.semantic import native_compare

    prompts = []

    class Model:
        def text_engine(self, messages, **kwargs):
            prompts.append(messages)
            if any("<subtask>" in m["content"] for m in messages):
                return "<subtask>Notify the recipient.</subtask>", None
            return "[[Success]] [[True]]", None

    result = native_compare(
        task(),
        {"tool": "EmailClientApp__send_email", "stage": 0},
        {
            "fields": ["subject", "body"],
            "actual": {"subject": "Hello", "body": "Hello from Alice"},
            "expected": {"subject": "Greeting", "body": "Greetings from Alice"},
        },
        Model(),
        {"first_name": "Alice", "last_name": "Smith", "address": "Home"},
    )
    assert result is True
    assert prompts and all(row[0]["role"] == "system" for row in prompts)


def test_summary_pools_counts_and_keeps_evidence_profiles_separate():
    from tau2.diagnostics.suite import summarize

    rows = [
        {
            "profile": "a",
            "summary": {
                "measures": {},
                "coverage": {"total_stages": 3, "delivered_stages": 2},
            },
        },
        {
            "profile": "a",
            "summary": {
                "measures": {},
                "coverage": {"total_stages": 3, "delivered_stages": 3},
            },
        },
    ]
    report = summarize(rows)
    assert report["total_trials"] == 2
    assert report["stage_coverage"]["rate"] == 5 / 6
    rows[1]["profile"] = "b"
    report = summarize(rows)
    assert set(report["groups"]) == {"a", "b"}
    assert "intent" not in report


def test_native_semantic_unknown_is_not_a_failed_parameter():
    pytest.importorskip("bs4")
    from tau2.diagnostics.execution.semantic import native_compare

    class Unknown:
        def text_engine(self, *args, **kwargs):
            return "No reliable verdict can be established.", None

    result = native_compare(
        task(),
        {"tool": "EmailClientApp__send_email", "stage": 0},
        {
            "fields": ["subject", "body"],
            "actual": {"subject": "A", "body": "A"},
            "expected": {"subject": "B", "body": "B"},
        },
        Unknown(),
        {"first_name": "Alice", "last_name": "Smith"},
    )
    assert result is None


def test_application_instances_never_match_each_other():
    tools = {
        "RentAFlat__save_apartment": {"write": True, "app": "RentAFlat"},
        "RentAFlat__remove_saved_apartment": {"write": True, "app": "RentAFlat"},
    }
    e = evidence()
    e.operations[0].app = "OtherApp"
    result = Gaia2ExecutionAdapter(tools).evaluate(task(), e, None)
    assert result["dimensions"]["tools"]["matched"] == 1
    assert result["dimensions"]["tools"]["recall"] == 0.5


def test_equal_time_receipt_binding_is_unknown_not_incorrect():
    from tau2.diagnostics.execution.bindings import ReferenceBindings

    refs = [
        {"id": "create", "args": {}},
        {"id": "consume", "args": {"conversation_id": "{{create}}"}},
    ]
    actual = [
        {"id": "c", "time": 1, "succeeded": True, "return_value": "new-id"},
        {"id": "d", "time": 1, "succeeded": True, "return_value": None},
    ]
    mapping = {"alternatives": {0: [0], 1: [1]}, "pairs": {0: 0, 1: 1}}
    args, errors = ReferenceBindings(refs, actual, mapping).arguments(
        refs[1], actual[1]
    )
    assert errors["conversation_id"]["passed"] is None


def test_read_receipts_resolve_participants_without_read_path_scoring():
    t = task()
    read = ScenarioAction(
        id="lookup",
        kind="reference",
        tool="RentAFlat__lookup_id",
        arguments={"query": "home"},
        dependencies=["u0"],
    )
    t.scenario.events.insert(1, read)
    t.scenario.events[2].arguments = {"apartment_id": "{{lookup}}"}
    t.scenario.events[2].dependencies = ["lookup"]
    e = evidence()
    e.operations.insert(
        0,
        OperationEvidence(
            call_id="read",
            name=read.tool,
            app="RentAFlat",
            kind="READ",
            raw_arguments=read.arguments,
            arguments=read.arguments,
            result="a",
            started=1000.5,
            ended=1000.6,
        ),
    )
    tools = {
        read.tool: {"write": False, "app": "RentAFlat"},
        "RentAFlat__save_apartment": {"write": True, "app": "RentAFlat"},
        "RentAFlat__remove_saved_apartment": {"write": True, "app": "RentAFlat"},
    }
    result = Gaia2ExecutionAdapter(tools).evaluate(t, e, None)
    assert result["dimensions"]["parameters"]["score"] == 1
    assert result["actual_count"] == 2


def test_reference_default_parameters_are_applied_to_actual_calls():
    t = task()
    e = evidence()
    t.scenario.events[1].tool = "Files__mv"
    t.scenario.events[1].arguments = {"path1": "/a", "path2": "/b"}
    e.operations[0] = OperationEvidence(
        call_id="c0",
        name="Files__mv",
        app="Files",
        kind="WRITE",
        raw_arguments={"path1": "/a", "path2": "/b"},
        arguments={"path1": "/a", "path2": "/b"},
        started=1001,
        ended=1002,
    )
    tools = {
        "Files__mv": {"write": True, "app": "Files", "defaults": {"recursive": False}},
        "RentAFlat__remove_saved_apartment": {"write": True, "app": "RentAFlat"},
    }
    result = Gaia2ExecutionAdapter(tools).evaluate(t, e, None)
    checks = result["dimensions"]["parameters"]["units"]
    assert next(r for r in checks if r["parameter"] == "recursive")["passed"] is True

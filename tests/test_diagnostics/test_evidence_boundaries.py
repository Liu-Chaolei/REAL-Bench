"""Small saved-evidence boundary checks, without providers or model calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tau2.data_model.diagnostics import DiagnosticEvidence, UserInput
from tau2.data_model.message import Tick, UserMessage
from tau2.data_model.tasks import Task, UserScenario
from tau2.diagnostics.collect import delivered_inputs, runtime_facts
from tau2.diagnostics.conciseness.evaluator import _audio_stats
from tau2.diagnostics.media import read_media_activity
from tau2.diagnostics.request_changes import (
    feedback_evidence,
    management_events,
    request_relations,
)
from tau2.metrics.active.persistent_task.evaluator import score_persistent_task
from tau2.metrics.active.persistent_task.variant_evaluator import (
    score_interaction_variant,
)


@pytest.mark.parametrize("voice", [False, True])
def test_delivery_source_survives_partial_delivery_and_retry(voice):
    messages = [
        UserMessage(
            role="user",
            content="Change it",
            contains_speech=True,
            utterance_ids=[str(i)],
            is_final_chunk=bool(i),
            raw_data={"scenario_input_id": "r"},
        )
        for i in range(2)
    ]
    simulation = SimpleNamespace(
        ticks=[
            Tick(tick_id=i, timestamp="now", user_chunk=m, tick_duration_seconds=0.2)
            for i, m in enumerate(messages)
        ]
        if voice
        else [],
        messages=messages,
        info={},
        environment_trace=[
            {
                "kind": "user",
                "event_id": "r",
                "source": "interaction_variant",
                "purpose": "followup",
            }
        ],
    )
    rows = delivered_inputs(simulation, None)
    assert len(rows) == 2
    assert all(
        (r.source, r.purpose) == ("interaction_variant", "followup") for r in rows
    )
    if voice:
        assert [r.complete for r in rows] == [False, True]
    simulation.environment_trace = []
    assert all(r.source is None for r in delivered_inputs(simulation, None))


@pytest.mark.parametrize("location", ["old", "new", "both"])
def test_variant_readers_use_current_facts_with_one_legacy_fallback(location):
    summary = {
        "variant_type": "modify",
        "operations": [
            {
                "call_id": "c",
                "operation": "modify",
                "timestamp": 1,
                "result": {"accepted": True},
            }
        ],
        "requests": [
            {
                "request_id": "r",
                "previous_request_id": "initial",
                "delivery": {"delivered_at": 104},
            }
        ],
    }
    source = {}
    if location != "new":
        source["active_metrics"] = {"metrics": {"persistent_task": deepcopy(summary)}}
    if location != "old":
        source["scenario_mechanisms"] = {"mechanisms": {"interaction_variant": summary}}
    if location == "both":
        source["active_metrics"]["metrics"]["persistent_task"]["operations"] = []
        source["active_metrics"]["metrics"]["persistent_task"]["requests"] = []
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        source=source,
        user_inputs=[
            UserInput(id="u", text="Initial", started=0, ended=1, stage=0),
            UserInput(id="r", event_id="r", text="Change", started=4, ended=5, stage=0),
        ],
        stages={"stages": [{"stage": 0, "started": {"time": 100}, "completed": True}]},
    )
    assert len(management_events(evidence)) == 1
    assert request_relations(evidence)["r"]["previous_request_id"] == "initial"
    assert [r.stage for r in feedback_evidence(evidence).user_inputs] == [0, 1]
    assert evidence.user_inputs[1].stage == 0


def test_runtime_fact_copy_excludes_scores_and_does_not_alias_results():
    envelope = {
        "version": "v1",
        "metrics": {"persistent_task": {"operations": []}},
        "scores": {"persistent_task": {"completion_score": 0.0}},
    }
    facts = runtime_facts(envelope)
    assert "scores" not in facts
    envelope["metrics"]["persistent_task"]["operations"].append("late")
    assert facts["metrics"]["persistent_task"]["operations"] == []


@pytest.mark.parametrize(
    "intervals,expected", [([[1, 2], [1.5, 2.5]], 1.5), ([], 0.0), (None, None)]
)
def test_audio_facts_do_not_require_user_classification_or_voice_scores(
    tmp_path, monkeypatch, intervals, expected
):
    def forbidden(*args):
        pytest.fail("Audio facts must not invoke a voice scorer")

    monkeypatch.setattr("tau2.diagnostics.voice.evaluate_voice", forbidden)
    activity = {} if intervals is None else {"agent_intervals": intervals}
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        audio={
            "status": "collected",
            "duration": 4,
            "activity": activity,
            "timeline": [{"start": 0, "end": 4, "agent": {"bytes": 1}}],
        },
    )
    result = _audio_stats(
        evidence,
        tmp_path,
        [{"id": "w", "start": 0, "end": 4, "replies": [{"started": 0, "ended": 4}]}],
    )["w"]
    if expected is None:
        assert result["status"] == "unavailable"
    else:
        assert result["agent_speech_seconds"] == expected
        assert result["agent_playback_seconds"] == 4


def test_historical_audio_uses_existing_vad_and_preserves_channel_gaps(
    tmp_path, monkeypatch
):
    from tau2.data_model.audio import AudioEncoding, AudioFormat

    fmt = AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1)
    (tmp_path / "agent.audio").write_bytes(bytes(64000))
    monkeypatch.setattr(
        "tau2.voice.audio_activity.detect_speech", lambda *args: [[0.5, 1.5]]
    )
    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        audio={
            "status": "collected",
            "duration": 5,
            "files": {"agent": "agent.audio"},
            "timeline": [
                {
                    "start": a,
                    "end": a + 1,
                    "agent": {
                        "offset": i * 32000,
                        "bytes": 32000,
                        "format": fmt.model_dump(mode="json"),
                    },
                }
                for i, a in enumerate([1, 4])
            ],
        },
    )
    media = read_media_activity(evidence, tmp_path)
    assert media["agent_intervals"] == [[1.5, 2], [4, 4.5]]
    assert media["time_basis"] == "media_relative_seconds"


@pytest.mark.parametrize("variant", [False, True])
def test_json_summary_scores_without_a_runtime(variant):
    summary = {
        "status": "completed",
        "binding": {"backend_task_id": "t", "task_label": "work"},
        "delivery": {"delivered_at": 1},
        "reply_text": "Updated",
        "resolved_intent": {"operation": "modify"},
        "operations": [
            {
                "backend_task_id": "t",
                "operation": "modify",
                "timestamp": 2,
                "kind": "native_operation_completed",
                "result": {"accepted": True},
            }
        ],
    }
    if variant:
        summary.update(variant_type="modify", binding_status={"primary": "bound"})
    loaded = json.loads(json.dumps(summary))
    grader = score_interaction_variant if variant else score_persistent_task
    assert grader(loaded)["completion_score"] == 1
    assert loaded == summary


def test_suite_regrades_saved_facts_without_an_embedded_score(tmp_path):
    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.suite import evaluate_evidence

    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        source={
            "active_metrics": {
                "metrics": {"persistent_task": {"status": "unsupported"}}
            }
        },
    )
    result = evaluate_evidence(
        Task(id="t", user_scenario=UserScenario(instructions="work")),
        evidence,
        tmp_path / "evidence",
        MetricsConfig(measures=["persistent_task"]),
    )
    assert result["summary"]["measures"]["persistent_task"]["status"] == "unsupported"


def test_saved_diagnostic_package_contains_runtime_facts_and_request_source(tmp_path):
    from tau2.data_model.simulation import SimulationRun
    from tau2.diagnostics.collect import finalize_evidence
    from tau2.diagnostics.suite import load_evidence

    simulation = SimulationRun(
        id="s",
        task_id="t",
        start_time="a",
        end_time="b",
        duration=1,
        termination_reason="user_stop",
        messages=[
            UserMessage(
                role="user", content="Change", raw_data={"scenario_input_id": "r"}
            )
        ],
        environment_trace=[
            {
                "kind": "user",
                "event_id": "r",
                "source": "interaction_variant",
                "purpose": "followup",
            }
        ],
        info={
            "scenario_mechanisms": {
                "mechanisms": {
                    "interaction_variant": {"variant_type": "modify", "requests": []}
                },
                "scores": {"interaction_variant": {"completion_score": 1}},
            },
        },
    )
    recorder = SimpleNamespace(
        close=lambda: None,
        media_ticks=[],
        operations=[],
        frames={},
        state_events=[],
        errors=[],
        initial_frame=None,
        frame_labels={},
    )
    orchestrator = SimpleNamespace(
        task=Task(id="t", user_scenario=UserScenario(instructions="work")),
        agent=SimpleNamespace(),
        environment=SimpleNamespace(
            get_domain_name=lambda: "gaia2", tools=SimpleNamespace(tools={})
        ),
    )
    finalize_evidence(orchestrator, simulation, recorder, tmp_path)
    saved = load_evidence(tmp_path)
    assert saved.user_inputs[0].source == "interaction_variant"
    assert "scores" not in saved.source["scenario_mechanisms"]
    assert (
        saved.source["scenario_mechanisms"]["mechanisms"]["interaction_variant"][
            "variant_type"
        ]
        == "modify"
    )
    assert (
        simulation.info["scenario_mechanisms"]["scores"]["interaction_variant"][
            "completion_score"
        ]
        == 1
    )

"""Waveform/timeline and aggregate correctness, independent of model services."""

import hashlib
import io
import math
import struct
import wave

import pytest

from tau2.data_model.diagnostics import OperationEvidence
from tau2.diagnostics.media_clock import map_scenario_time_to_media
from tau2.diagnostics.voice import map_tool_operations
from tau2.metrics import real_voice as im


def evidence(user=None, agent=None, segments=None):
    agent = agent or []
    return {
        "version": im.EVIDENCE_VERSION,
        "config": dict(im.CONFIG),
        "status": "valid",
        "input_start_mono": 1.0,
        "cutoff_mono": 100.0,
        "user_intervals": user or [[2.0, 4.0]],
        "agent_intervals": agent,
        "agent_segments": segments if segments is not None else agent,
    }


def call(start, end):
    return {"started_mono": start, "completed_mono": end, "completed": True}


def value(report, name):
    return report["metrics"][name]["value"]


def wav_file(path, pcm, rate=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    path.write_bytes(stream.getvalue())


def voiced_pcm(rate, seconds):
    return b"".join(
        (
            struct.pack(
                "<h",
                int(
                    10000 * math.sin(2 * math.pi * 180 * i / rate)
                    + 3000 * math.sin(2 * math.pi * 360 * i / rate)
                ),
            )
            for i in range(int(rate * seconds))
        )
    )


def test_response_latency_and_no_response_are_paired():
    report = im.score_interactions(evidence(agent=[[5.0, 6.0]]), [])
    assert value(report, "response_latency_mean") == 1.0
    assert value(report, "response_rate") == 1.0
    assert value(report, "on_time_rate") == 1.0
    assert value(report, "late_rate") == 0.0
    assert value(report, "agent_interruption_rate") == 0.0
    assert value(report, "speech_overlap_duration") == 0.0
    silent = im.score_interactions(evidence(), [])
    assert value(silent, "response_rate") == 0.0
    assert value(silent, "response_latency_mean") is None
    assert silent["metrics"]["response_latency_mean"]["reason"] == "no_response"
    assert value(silent, "audible_pretoolspeech_rate") is None


@pytest.mark.parametrize(
    "latency,tools,on_time,late",
    [
        (0.0, False, 0.0, 0.0),
        (0.199, False, 0.0, 0.0),
        (0.2, False, 1.0, 0.0),
        (2.749, False, 1.0, 0.0),
        (2.75, False, 0.0, 1.0),
        (2.75, True, 1.0, 0.0),
        (3.999, True, 1.0, 0.0),
        (4.0, True, 0.0, 1.0),
    ],
)
def test_eva_latency_threshold_boundaries(latency, tools, on_time, late):
    report = im.score_interactions(
        evidence(agent=[[4 + latency, 5 + latency]]), [call(2, 3)] if tools else []
    )
    assert value(report, "on_time_rate") == on_time
    assert value(report, "late_rate") == late


def test_multiple_barge_ins_count_one_turn_and_overlap_excludes_user_pauses():
    report = im.score_interactions(
        evidence(user=[[2, 3], [3.5, 4]], agent=[[2.5, 2.8], [3.7, 4.5]]), []
    )
    assert value(report, "agent_interruption_rate") == 1.0
    assert value(report, "speech_overlap_duration") == pytest.approx(0.6)
    assert value(report, "response_rate") is None
    assert report["details"]["excluded_response_turns"] == 1
    assert report["metrics"]["response_rate"]["reason"] == "agent_speaking_at_user_end"


def test_endpoint_touch_is_not_overlap_or_interruption():
    report = im.score_interactions(evidence(agent=[[4, 5]]), [])
    assert value(report, "speech_overlap_duration") == 0
    assert value(report, "agent_interruption_rate") == 0
    assert value(report, "response_latency_mean") == 0


def test_tool_groups_use_audible_intervals_and_reset_after_previous_group():
    report = im.score_interactions(
        evidence(agent=[[4.5, 5.0], [8, 8.5], [12, 13]]),
        [call(6, 7), call(7.1, 7.5), call(9, 10)],
    )
    assert value(report, "audible_pretoolspeech_rate") == 1
    assert [group["tool_count"] for group in report["details"]["tool_groups"]] == [2, 1]
    silent_lead = im.score_interactions(
        evidence(agent=[[8, 8.5]]), [call(6, 7), call(9, 10)]
    )
    assert value(silent_lead, "audible_pretoolspeech_rate") == 0.5
    invalid = im.score_interactions(evidence(agent=[[5, 6]]), [call(6, 101)])
    assert value(invalid, "audible_pretoolspeech_rate") is None
    assert (
        invalid["metrics"]["audible_pretoolspeech_rate"]["reason"]
        == "invalid_tool_timeline"
    )
    assert value(invalid, "response_latency_mean") == 1


def test_missing_tool_context_does_not_guess_non_tool_threshold():
    report = im.score_interactions(evidence(agent=[[7, 8]]))
    assert value(report, "response_latency_mean") == 3
    assert value(report, "on_time_rate") is None
    assert report["metrics"]["on_time_rate"]["reason"] == "missing_tool_context"


def test_aggregation_pools_samples_instead_of_trial_percentiles_and_ratios():
    short = im.score_interactions(evidence(agent=[[5, 6]]), [call(6, 7)])
    long = im.score_interactions(
        evidence(agent=[[13, 14]]), [call(6, 7), call(14.5, 15)]
    )
    summary = im.aggregate_interactions(
        [short, long, im.score_interactions(evidence(), [])]
    )
    assert value(summary, "response_latency_mean") == 5
    assert value(summary, "response_latency_p90") == pytest.approx(8.2)
    assert value(summary, "response_rate") == pytest.approx(2 / 3)
    assert value(summary, "speech_overlap_duration") == 0
    assert value(summary, "audible_pretoolspeech_rate") == pytest.approx(2 / 3)
    assert summary["metrics"]["audible_pretoolspeech_rate"]["unavailable_trials"] == 1


def test_incompatible_configs_cannot_be_aggregated():
    report = im.score_interactions(evidence(), [])
    report["config"] = {**report["config"], "late_threshold_sec": 5}
    with pytest.raises(ValueError, match="different interaction metric"):
        im.aggregate_interactions([report])


def test_mapping_preserves_queue_gaps_and_partial_sample_boundaries():
    timeline = [
        {"sample_offset": 0, "sample_count": 320, "start_mono": 5, "end_mono": 5.02},
        {"sample_offset": 320, "sample_count": 160, "start_mono": 6, "end_mono": 6.01},
    ]
    assert im.map_speech([[0, 0.03]], timeline, 16000, 480) == [[5, 5.02], [6, 6.01]]
    with pytest.raises(ValueError, match="count_mismatch"):
        im.map_speech([[0, 0.03]], timeline, 16000, 481)
    with pytest.raises(ValueError, match="missing_sample_timeline"):
        im.map_speech([[0, 0.03]], [], 16000, 480)


def test_media_clock_maps_tick_interiors_gaps_boundaries_and_rejects_outside():
    timeline = [
        {"start": 0, "end": 1, "monotonic_start": 100, "monotonic_end": 101},
        {"start": 1.5, "end": 2.5, "monotonic_start": 102, "monotonic_end": 103},
    ]
    inside = map_scenario_time_to_media(1000.5, timeline, 1000, 100)
    gap = map_scenario_time_to_media(1001.5, timeline, 1000, 100)
    boundary = map_scenario_time_to_media(1001, timeline, 1000, 100)
    outside = map_scenario_time_to_media(999, timeline, 1000, 100)
    assert inside["status"] == "mapped" and inside["source"] == "tick"
    assert gap["status"] == "mapped" and gap["source"] == "gap_boundary"
    assert gap["media_seconds"] == pytest.approx(1.25)
    assert boundary["media_seconds"] == pytest.approx(1)
    assert outside == {
        "status": "unavailable",
        "media_seconds": None,
        "source": None,
        "distance_seconds": None,
        "reason": "outside_media_timeline",
    }


def test_tool_mapping_keeps_good_calls_when_one_operation_is_unavailable():
    def operation(call_id, started, ended):
        return OperationEvidence(
            call_id=call_id,
            name="Files__ls",
            app="Files",
            kind="READ",
            raw_arguments={},
            arguments={},
            started=started,
            ended=ended,
        )

    timeline = [
        {"start": 0, "end": 1, "monotonic_start": 100, "monotonic_end": 101},
        {"start": 1, "end": 2, "monotonic_start": 102, "monotonic_end": 103},
    ]
    calls, context = map_tool_operations(
        [operation("good", 1000.2, 1001.5), operation("bad", 999, 999.5)],
        timeline,
        1000,
        100,
    )
    assert [call["call_id"] for call in calls] == ["good"]
    assert context["total_operations"] == 2
    assert context["mapped_operations"] == 1
    assert context["unmapped_operations"] == 1


@pytest.mark.parametrize("rate", [16000, 24000])
def test_real_vad_resampling_silence_and_voiced_boundaries(rate):
    pcm = (
        bytes(int(rate * 0.4) * 2) + voiced_pcm(rate, 0.8) + bytes(int(rate * 0.8) * 2)
    )
    pytest.importorskip("soxr")
    pytest.importorskip("webrtcvad")
    intervals = im.detect_speech(pcm, rate)
    assert intervals
    assert intervals[0][0] >= 0.38
    assert intervals[-1][1] < 1.6
    assert im.detect_speech(bytes(rate * 2), rate) == []


def test_audio_evidence_excludes_startup_and_uses_wav_not_transcripts(tmp_path):
    pytest.importorskip("soxr")
    pytest.importorskip("webrtcvad")
    wav_file(tmp_path / "input.wav", voiced_pcm(16000, 0.8) + bytes(16000))
    wav_file(tmp_path / "response.wav", bytes(24000 * 2), 24000)
    digest = hashlib.sha256((tmp_path / "response.wav").read_bytes()).hexdigest()
    observation = {
        "input_complete": True,
        "input_start_mono": 1,
        "cutoff_mono": 10,
        "input_timeline": [
            {
                "sample_offset": 0,
                "sample_count": 20800,
                "start_mono": 1,
                "end_mono": 2.3,
            }
        ],
        "responses": [
            {"response_id": "startup", "phase": "startup"},
            {
                "response_id": "r",
                "phase": "task",
                "audio_file": "response.wav",
                "audio_sha256": digest,
                "played_samples": 24000,
                "sample_rate": 24000,
                "transcript": "This is not audible.",
                "playback_timeline": [
                    {
                        "sample_offset": 0,
                        "sample_count": 24000,
                        "start_mono": 4,
                        "end_mono": 5,
                    }
                ],
            },
        ],
    }
    result = im.analyze_audio(tmp_path / "input.wav", tmp_path, observation)
    assert result["status"] == "valid"
    assert result["excluded_startup_responses"] == 1
    assert result["agent_intervals"] == []
    assert result["user_intervals"][-1][1] < 2.3
    assert value(im.score_interactions(result, []), "response_rate") == 0
    observation["responses"][1]["audio_sha256"] = "damaged"
    assert (
        im.analyze_audio(tmp_path / "input.wav", tmp_path, observation)["reason"]
        == "output_audio_hash_mismatch"
    )

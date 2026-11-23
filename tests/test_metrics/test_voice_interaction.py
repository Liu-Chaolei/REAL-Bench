"""Hand-computed voice cases; no providers, recordings or model calls."""

from copy import deepcopy

import pytest

from tau2.metrics.voice_interaction import (
    EVENT_VERSION,
    METRIC_FIELDS,
    aggregate,
    evaluate,
    group_speech,
)


def event(id, start, end, kind="formal"):
    return dict(id=id, kind=kind, intervals=[[start, end]])


def score(events, agent, cutoff=10, calls=(), key=("mock", "en", "base", "task")):
    return evaluate(
        dict(
            user_event_version=EVENT_VERSION,
            user_events=events,
            agent_intervals=agent,
            duration=cutoff,
        ),
        list(calls),
        key,
    )


def value(report, name):
    return report["metrics"][name]["value"]


def test_formal_denominator_and_last_unanswered_turn():
    report = score(
        [
            event("a", 0, 1),
            event("bc", 1.2, 1.3, "backchannel"),
            event("b", 3, 4),
            event("noise", 4.2, 4.4, "background"),
            event("c", 6, 7),
        ],
        [[1.5, 2]],
    )
    assert value(report, "response_rate") == pytest.approx(1 / 3)
    assert value(report, "response_latency_mean") == 0.5
    assert len(report["evaluation_log"]["responses"]) == 3
    assert sum(r["responded"] for r in report["evaluation_log"]["responses"]) == 1


def test_continuing_audio_does_not_answer_formal_turns_at_zero():
    report = score([event("a", 0.2, 0.5), event("b", 0.8, 1.1)], [[0, 2]])
    assert value(report, "response_rate") == 0
    assert value(report, "response_latency_mean") is None
    assert value(report, "response_latency_p90") is None
    assert value(report, "on_time_rate") is None
    assert value(report, "agent_interruption_event_rate") == 0


def test_actual_silence_is_not_a_zero_latency_response():
    report = score([event("a", 0.2, 1.05)], [[0, 1], [1.1, 2]])
    assert value(report, "response_rate") == 0
    assert value(report, "response_latency_mean") is None
    assert group_speech([[0, 1], [1.1, 2]]) == [[0, 2]]


def test_new_agent_group_after_user_end_is_the_response():
    report = score([event("a", 0.2, 1.05)], [[0, 1], [1.3, 2]])
    assert group_speech([[0, 1], [1.3, 2]]) == [[0, 1], [1.3, 2]]
    assert value(report, "response_rate") == 1
    assert value(report, "response_latency_mean") == pytest.approx(0.25)
    assert value(report, "response_latency_p90") == pytest.approx(0.25)


def test_agent_interrupting_and_continuing_is_not_a_response():
    report = score([event("a", 1, 2)], [[1.5, 3]])
    assert value(report, "response_rate") == 0
    assert value(report, "response_latency_mean") is None
    assert value(report, "agent_interrupted_turn_rate") == 1


def test_no_waiting_formal_turn_does_not_create_responses():
    report = score([event("c", 1, 1.2, "vocal_tic")], [[1.5, 2]])
    assert value(report, "response_rate") is None
    assert report["evaluation_log"]["responses"] == []
    assert value(report, "selectivity_vocal_tic") == 0


def test_late_yield_has_no_two_second_deadline():
    report = score([event("u", 0.5, 1.8)], [[0, 1], [1.3, 3.5]], cutoff=5)
    assert value(report, "yield_rate") == 1
    assert value(report, "yield_latency_mean") == 3
    stop = report["evaluation_log"]["yields"][0]
    assert stop["stopped"] == 3.5 and stop["confirmed"] == 4.5


def test_tail_silence_is_unknown_but_continued_speech_is_not_yield():
    unknown = score([event("u", 1, 1.5)], [[0, 2]], cutoff=2.6)
    assert value(unknown, "yield_rate") is None
    assert unknown["evaluation_log"]["counts"]["yield_observation_insufficient"] == 1
    continued = score([event("u", 1, 1.5)], [[0, 3]], cutoff=3)
    assert value(continued, "yield_rate") == 0


def test_two_interruption_rates_and_real_overlap():
    report = score(
        [event("a", 0, 3), event("b", 4, 6)], [[0.5, 1], [1.3, 2], [2.3, 2.5]]
    )
    assert value(report, "agent_interruption_event_rate") == 1.5
    assert value(report, "agent_interrupted_turn_rate") == 0.5
    assert value(report, "speech_overlap_duration") == pytest.approx(0.7)


@pytest.mark.parametrize("kind", ["backchannel", "vocal_tic", "non_directed"])
def test_selectivity_confirms_one_second_after_candidate_window(kind):
    report = score([event("e", 0.5, 0.7, kind)], [[0, 1.1]], cutoff=3)
    assert value(report, "selectivity_" + kind) == 0
    stop = report["evaluation_log"]["selectivity"][0]
    assert stop["stopped"] == 1.1 and stop["confirmed"] == 2.1
    resumed = score([event("e", 0.5, 0.7, kind)], [[0, 1.1], [1.9, 2.5]], cutoff=4)
    assert value(resumed, "selectivity_" + kind) == 1


@pytest.mark.parametrize(
    "delay,tool,timely",
    [(0, False, 1), (0.1, False, 1), (2.75, False, 0), (2.75, True, 0), (4, True, 0)],
)
def test_on_time_limits_without_early_penalty(delay, tool, timely):
    calls = [dict(started_mono=0.5)] if tool else []
    report = score([event("u", 0, 1)], [[1 + delay, 1.2 + delay]], calls=calls)
    assert value(report, "on_time_rate") == timely


def with_samples(samples, key):
    report = score([], [], key=key)
    report["evaluation_log"]["responses"] = [
        dict(responded=True, latency=v) for v in samples
    ]
    report["metrics"]["response_latency_mean"]["value"] = sum(samples) / len(samples)
    return report


def test_task_and_trial_equal_averages_and_weighted_p90():
    a = with_samples([0] * 99 + [10], ("mock", "en", "base", "a"))
    b = with_samples([5], ("mock", "en", "base", "b"))
    pooled = aggregate([a, b])
    assert value(pooled, "response_latency_mean") == 2.55
    assert value(pooled, "response_latency_p90") == 5
    a2 = with_samples([10], ("mock", "en", "base", "a"))
    pooled = aggregate([with_samples([0] * 100, a["task_key"]), a2, b])
    assert value(pooled, "response_latency_mean") == 5
    assert value(pooled, "response_latency_p90") == 10
    assert set(pooled["metrics"]) == set(METRIC_FIELDS)


def test_historical_evidence_is_not_relabelled():
    old = dict(
        user_turns=[dict(intervals=[[0, 1]])], agent_intervals=[[2, 3]], duration=5
    )
    report = evaluate(old)
    assert report["status"] == "unavailable"
    assert all(v["value"] is None for v in report["metrics"].values())


def test_original_input_intervals_are_not_filled_in_place():
    agent = [[0, 1], [1.1, 2]]
    expected = deepcopy(agent)
    score([event("a", 0.2, 1.05)], agent)
    assert agent == expected


def test_classified_media_flows_through_all_output_entries(tmp_path):
    import base64
    from types import SimpleNamespace

    from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT
    from tau2.data_model.diagnostics import DiagnosticEvidence
    from tau2.data_model.message import AssistantMessage, Tick, UserMessage
    from tau2.diagnostics.collect import collect_audio
    from tau2.diagnostics.suite import summarize
    from tau2.diagnostics.voice import evaluate_voice
    from tau2.metrics.interaction_quality import measure_interaction
    from tau2.metrics.voice_interaction_metrics import (
        compute_interaction_metrics_for_experiment,
    )
    from tau2.scripts.leaderboard.compute_interaction_metrics import (
        build_interaction_metrics_block,
    )
    from tau2.scripts.leaderboard.submission import InteractionMetrics

    fmt = TELEPHONY_AUDIO_FORMAT.model_dump(mode="json")

    def packet(a, b, kind, uid):
        return dict(
            role="user",
            start=a,
            end=b,
            speech_intervals=[[a, b]],
            utterance_ids=[uid],
            user_event_kind=kind,
            user_event_version=EVENT_VERSION,
            format=fmt,
            audio=base64.b64encode(b"\xff" * 80).decode(),
        )

    packets = [
        packet(10, 10.5, "formal", "u"),
        packet(10.5, 11, "formal", "u"),
        packet(11.2, 11.4, "backchannel", "bc"),
        packet(12, 12.1, "control", "stop"),
    ]
    packets[1]["independent_user_events"] = [
        dict(id="cough", kind="vocal_tic", intervals=[[10.7, 10.8]])
    ]
    tick = Tick(
        tick_id=0,
        timestamp="2026-11-21",
        tick_duration_seconds=0.2,
        user_chunk=UserMessage(role="user", content="Request"),
        agent_chunk=AssistantMessage(
            role="assistant",
            content="Answer",
            raw_data=dict(
                media_origin=10,
                media_cutoff=15,
                speech_intervals=[[11.5, 12]],
                media_timeline=packets,
            ),
        ),
    )
    sim = SimpleNamespace(ticks=[tick], task_id="a", language_context=None)
    audio = collect_audio(sim, tmp_path)
    assert (
        len(audio["activity"]["user_events"]) == 3
    )  # one formal, one continuer, one cough
    evidence = DiagnosticEvidence(
        simulation_id="s", task_id="a", domain="mock", audio=audio
    )
    report = evaluate_voice(evidence, tmp_path)
    assert value(report, "response_rate") == 1
    assert report["evaluation_log"]["counts"]["formal_turns"] == 1
    assert value(report, "response_latency_mean") == 0.5
    assert measure_interaction(sim).response_total == 1
    summary = summarize(
        [dict(task_id="a", domain="mock", summary=dict(measures={"voice": report}))]
    )
    assert "voice_interaction" in summary["metrics"]
    assert "real_voice" not in summary["metrics"]
    info = SimpleNamespace(
        model_dump=lambda **_: dict(
            environment_info={"domain_name": "mock"}, language="en"
        )
    )
    panel = compute_interaction_metrics_for_experiment(
        SimpleNamespace(info=info, simulations=[sim])
    )
    assert panel["response_rate"] == 1 and "agent_interruption_rate" not in panel
    block = build_interaction_metrics_block({"mock": panel})
    assert block["overall"]["response_latency_p90"] == 0.5
    InteractionMetrics.model_validate(block)
    mixed = build_interaction_metrics_block(
        {"mock": panel, "retail": {"response_rate": 0}}
    )
    assert mixed["overall"]["response_rate"] == 1
    assert mixed["domains"]["retail"]["version"] == "2.0"
    assert mixed["overall"]["evaluation_log"]["historical_domains"] == ["retail"]

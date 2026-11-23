"""Small offline checks for the four agreed metric-output changes."""

from copy import deepcopy
from io import StringIO

import pytest
from rich.console import Console

from tau2.data_model.simulation import RewardInfo, SimulationRun
from tau2.data_model.tasks import RewardType
from tau2.diagnostics.suite import summarize
from tau2.diagnostics.voice import aggregate_voice
from tau2.metrics.outcome import env_assertion_value, result_metric
from tau2.metrics.voice_interaction import CONFIG, EVENT_VERSION, VERSION, evaluate
from tau2.persistent_tasks.scores import completion_score, consolidate_score
from tau2.scripts.leaderboard.benchmark_quality import BenchmarkQuality, score_call
from tau2.utils.display import ConsoleDisplay, MarkdownDisplay


def simulation(reward=None):
    return SimulationRun(
        id="s",
        task_id="t",
        start_time="a",
        end_time="b",
        duration=1,
        termination_reason="user_stop",
        messages=[],
        reward_info=reward,
        info={"evaluation_log": {"domain": "gaia2"}},
    )


def test_env_display_reads_component_not_combined_reward(monkeypatch):
    sim = simulation(
        RewardInfo(
            reward=0.2,
            reward_breakdown={
                RewardType.ENV_ASSERTION: 1,
                RewardType.PERSISTENT_TASK: 0.2,
            },
        )
    )
    assert result_metric(sim) == ("ENV_ASSERTION", 1)
    text = MarkdownDisplay.display_simulation(sim)
    assert text.count("**ENV_ASSERTION**:") == 1 and "**Reward**" not in text
    assert "1.0000" in text and ".2000" not in text
    stream = StringIO()
    monkeypatch.setattr(ConsoleDisplay, "console", Console(file=stream, width=160))
    ConsoleDisplay.display_simulation(sim, show_details=False)
    assert "ENV_ASSERTION" in stream.getvalue() and "Reward:" not in stream.getvalue()
    assert sim.reward_info.reward == 0.2  # existing trial aggregation still reads this


def test_missing_env_component_is_not_fabricated_from_total():
    sim = simulation(RewardInfo(reward=1))
    assert env_assertion_value(sim) is None
    assert "**ENV_ASSERTION**: unavailable" in MarkdownDisplay.display_simulation(sim)
    sim.evaluation_status = "failed"
    sim.reward_info.reward_breakdown = {RewardType.ENV_ASSERTION: 1}
    assert env_assertion_value(sim) is None
    sim.info = {}
    assert result_metric(sim)[0] == "Reward"


@pytest.mark.parametrize(
    "saved,expected",
    [
        ({"completion_score": 0, "reward": 1}, 0),
        ({"completion_score": None, "reward": 1}, None),
        ({"reward": 0, "native_reward": 1}, 0),
        ({"native_reward": 1}, 1),
    ],
)
def test_completion_reading_preserves_zero_and_null(saved, expected):
    original = deepcopy(saved)
    assert completion_score(saved) == expected
    assert consolidate_score(saved)["completion_score"] == expected
    assert saved == original


def test_old_components_are_details_and_task_mean_uses_only_completion():
    old = dict(
        reward=0,
        native_result_reward=1,
        reply_reward=0,
        checks=[{"operation": True}],
        reply_checks=[{"met": False}],
    )
    new = consolidate_score(old)
    assert (
        not {"reward", "native_result_reward", "reply_reward", "checks", "reply_checks"}
        & new.keys()
    )
    assert new["evaluation_log"]["native_result"]["passed"] is True
    assert new["evaluation_log"]["reply"]["passed"] is False
    rows = [
        dict(
            task_id="a", summary={"measures": {"persistent_task": dict(old, reward=1)}}
        )
    ] * 3
    rows += [dict(task_id="b", summary={"measures": {"persistent_task": new}})]
    assert summarize(rows)["metrics"]["persistent_task"] == {"completion_score": 0.5}


def test_same_latency_is_timely_regardless_of_tool_evidence():
    activity = dict(
        user_event_version=EVENT_VERSION,
        user_events=[dict(id="u", kind="formal", intervals=[[0, 1]])],
        agent_intervals=[[3.7, 4]],
        duration=5,
    )
    for calls in (None, [], [{"started_mono": 0.2}]):
        report = evaluate(activity, calls)
        assert report["metrics"]["on_time_rate"]["value"] == 1
        assert report["evaluation_log"]["responses"][0]["on_time_limit"] == 2.75
    activity["agent_intervals"] = [[3.75, 4]]
    assert evaluate(activity)["metrics"]["on_time_rate"]["value"] == 0
    assert "on_time_tool_sec" not in CONFIG


def test_saved_voice_versions_are_kept_separate_without_rescoring():
    activity = dict(
        user_event_version=EVENT_VERSION,
        user_events=[dict(id="u", kind="formal", intervals=[[0, 1]])],
        agent_intervals=[[2, 3]],
        duration=5,
    )
    new = evaluate(activity, task_key=("mock", "en", "base", "t"))
    old = deepcopy(new)
    old.update(
        version="tau-voice-interaction/3", config={**CONFIG, "on_time_tool_sec": 4}
    )
    old["metrics"]["on_time_rate"]["value"] = 0
    assert aggregate_voice([old])["version"] == old["version"]
    rows = [dict(task_id="t", summary={"measures": {"voice": r}}) for r in (old, new)]
    grouped = summarize(rows)
    assert len(grouped["groups"]) == 2
    assert {
        g["evaluation_log"]["voice"]["version"] for g in grouped["groups"].values()
    } == {old["version"], VERSION}


def test_call_provenance_does_not_measure_or_emit_flags(monkeypatch):
    def forbidden(*args):
        pytest.fail("Retired flag still extracted audio events")

    monkeypatch.setattr(
        "tau2.metrics.interaction_quality.measure_interaction", forbidden
    )
    call = score_call(simulation(RewardInfo(reward=1)))
    assert set(call.model_dump()) == {"simulation_id", "task_id", "evaluation_log"}
    historic = dict(
        schema_version="benchmark-quality-v3",
        benchmark_version="voice-v2",
        frame_revision="a" * 64,
        cells=[
            dict(
                language="en",
                domain="retail",
                metrics={"selectivity": 1},
                evaluation_log={
                    "calls": [
                        dict(simulation_id="s", task_id="t", flags={"selectivity": 1})
                    ]
                },
            )
        ],
    )
    loaded = BenchmarkQuality.model_validate(historic).model_dump()
    assert loaded["schema_version"] == "benchmark-quality-v4"
    assert "metrics" not in loaded["cells"][0]
    assert loaded["cells"][0]["evaluation_log"]["historical_metrics"] == {
        "selectivity": 1
    }

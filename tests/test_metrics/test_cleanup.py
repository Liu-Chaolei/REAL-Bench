"""Focused offline checks for retired calls and task-equal summaries."""

import pytest

from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.simulation import RewardInfo, SimulationRun
from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
from tau2.diagnostics.suite import summarize
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.orchestrator.modes import CommunicationMode


@pytest.mark.parametrize("mode", list(CommunicationMode))
@pytest.mark.parametrize("selection", list(EvaluationType))
def test_gaia2_never_calls_retired_evaluators(monkeypatch, mode, selection):
    import tau2.evaluator.evaluator as module

    task = Task(
        id="t",
        user_scenario=UserScenario(instructions="do"),
        evaluation_criteria=EvaluationCriteria(
            reward_basis=[
                RewardType.ENV_ASSERTION,
                RewardType.COMMUNICATE,
                RewardType.ACTION,
                RewardType.NL_ASSERTION,
            ],
            nl_assertions=["Do not score this historical constraint"],
        ),
    )
    original = task.model_dump()
    simulation = SimulationRun(
        id="s",
        task_id="t",
        start_time="a",
        end_time="b",
        duration=1,
        termination_reason="user_stop",
        messages=[],
        ticks=[],
    )

    def forbidden(**kwargs):
        pytest.fail("retired evaluator was called")

    for cls in (
        module.ActionEvaluator,
        module.FullDuplexActionEvaluator,
        module.NLAssertionsEvaluator,
        module.FullDuplexNLAssertionsEvaluator,
        module.CommunicateEvaluator,
        module.FullDuplexCommunicateEvaluator,
    ):
        monkeypatch.setattr(cls, "calculate_reward", forbidden)
    calls = []

    def environment(**kwargs):
        calls.append("env")
        return RewardInfo(reward=1, reward_breakdown={RewardType.ENV_ASSERTION: 1})

    for cls in (module.EnvironmentEvaluator, module.FullDuplexEnvironmentEvaluator):
        monkeypatch.setattr(cls, "calculate_reward", environment)
    monkeypatch.setattr(module.registry, "get_env_constructor", lambda _: lambda: None)
    result = evaluate_simulation(
        simulation,
        task,
        selection,
        False,
        "gaia2",
        mode,
        communicate_scoring=CommunicateScoring(scorer="substring"),
    )
    assert result.reward == 1
    retired = {RewardType.ACTION, RewardType.NL_ASSERTION, RewardType.COMMUNICATE}
    assert not retired & set(result.reward_breakdown)
    assert not retired & set(result.reward_basis)
    assert result.communicate_checks is None
    assert simulation.info["evaluation_log"]["effective_reward_basis"] == [
        "ENV_ASSERTION"
    ]
    assert task.model_dump() == original
    if selection in {
        EvaluationType.ACTION,
        EvaluationType.NL_ASSERTIONS,
        EvaluationType.COMMUNICATE,
    }:
        assert calls == []
    else:
        assert calls == ["env"]


def test_task_means_ignore_trial_count_and_keep_datasets_separate():
    def row(task, value, dataset="main"):
        return {
            "task_id": task,
            "task_set_name": dataset,
            "summary": {
                "measures": {
                    "intent": {
                        "method": "bidirectional_text",
                        "status": "complete",
                        "precision": value,
                        "recall": value,
                        "f1": value,
                    },
                }
            },
        }

    rows = [row("a", 1)] * 9 + [row("b", 0)]
    report = summarize(rows)
    assert report["metrics"]["intent"] == {"macro_f1": 0.5}
    assert "interaction" not in report["metrics"]
    assert report["evaluation_log"]["metric_details"]["intent"] == {
        "precision": 0.5,
        "recall": 0.5,
    }
    assert report["evaluation_log"]["total_trials"] == 10
    assert "coverage" not in report["metrics"]["intent"]
    assert "micro" not in report["metrics"]["intent"]
    rows.append(row("a", 0, "hidden"))
    assert summarize(rows)["metrics"]["intent"]["macro_f1"] == pytest.approx(1 / 3)


def test_backend_result_summary_reads_old_fields_without_overriding_new_zero():
    def row(identifier, result):
        return {"task_id": identifier, "persistent_tasks": {"grading": result}}

    report = summarize(
        [
            row("new", {"completion_score": 0.0, "reward": 1.0}),
            row("old", {"reward": 1.0}),
        ]
    )
    result = report["metrics"]["persistent_task"]
    assert result["completion_score"] == 0.5
    assert "state_result_reward" not in result
    # A present but unscored new field must not revive an old score either.
    result = summarize(
        [
            row(
                "null",
                {
                    "completion_score": None,
                    "reward": 1.0,
                },
            )
        ]
    )["metrics"]["persistent_task"]
    assert result["completion_score"] is None

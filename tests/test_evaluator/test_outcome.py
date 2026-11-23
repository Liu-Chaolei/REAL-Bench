"""Budget exhaustion grades real outcomes in both communication modes."""

from copy import deepcopy

import pytest

from tau2.data_model.message import AssistantMessage
from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.simulation import RewardInfo, SimulationRun, TerminationReason
from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.evaluator.outcome import GRADING_VERSION
from tau2.orchestrator.modes import CommunicationMode


@pytest.mark.parametrize("ending", list(TerminationReason))
@pytest.mark.parametrize("mode", list(CommunicationMode))
def test_budget_and_stop_endings_grade_actual_answer(monkeypatch, ending, mode):
    from tau2.evaluator.evaluator_communicate import FullDuplexCommunicateEvaluator

    task = Task(
        id="mock",
        user_scenario=UserScenario(instructions="Say done"),
        evaluation_criteria=EvaluationCriteria(
            communicate_info=["done"], reward_basis=[RewardType.COMMUNICATE]
        ),
    )
    simulation = SimulationRun(
        id="test",
        task_id=task.id,
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason=ending,
        messages=[AssistantMessage(role="assistant", content="done")],
        ticks=[],
    )
    calls = []

    def reward(**kw):
        calls.append(kw)
        return RewardInfo(reward=1)

    monkeypatch.setattr(FullDuplexCommunicateEvaluator, "calculate_reward", reward)
    result = evaluate_simulation(
        simulation,
        task,
        EvaluationType.COMMUNICATE,
        False,
        "mock",
        mode,
        communicate_scoring=CommunicateScoring(scorer="substring"),
    )
    eligible = ending in {
        TerminationReason.AGENT_STOP,
        TerminationReason.USER_STOP,
        TerminationReason.MAX_STEPS,
        TerminationReason.TIMEOUT,
    }
    assert result.reward == float(eligible)
    assert result.info["grading_version"] == GRADING_VERSION
    if mode == CommunicationMode.FULL_DUPLEX:
        assert bool(calls) == eligible


def test_missing_stage_is_logged_without_zeroing_total(monkeypatch):
    from tau2.data_model.scenario import ScenarioAction, ScenarioSpec, ScenarioStage
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator
    from tau2.evaluator.scenario_series import validate_delivery

    task = Task(
        id="series",
        user_scenario=UserScenario(instructions="do"),
        evaluation_criteria=EvaluationCriteria(reward_basis=[RewardType.COMMUNICATE]),
        scenario=ScenarioSpec(
            source_id="series",
            source_sha256="0" * 64,
            snapshot="0" * 64,
            category="test",
            start_time=0,
            events=[
                ScenarioAction(id=f"u{i}", kind="user", tool="u") for i in range(3)
            ],
            stages=[
                ScenarioStage(stage=i, request="do", event_ids=[f"u{i}"], criteria={})
                for i in range(3)
            ],
        ),
    )
    summary = {
        "version": "test",
        "stages": [
            {
                "stage": i,
                "event_id": f"u{i}",
                "started": {"time": i, "index": i, "sequence": i} if i < 2 else None,
                "completed": {"time": i, "index": i, "sequence": i} if i < 2 else None,
            }
            for i in range(3)
        ],
    }
    simulation = SimulationRun(
        id="series",
        task_id=task.id,
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason=TerminationReason.MAX_STEPS,
        messages=[],
        info={"scenario_series": summary},
    )
    seen = []

    def reward(**kw):
        seen.append(kw)
        return RewardInfo(
            reward=1,
            reward_breakdown={RewardType.COMMUNICATE: 1},
            info={"evidence": "retained"},
        )

    monkeypatch.setattr(CommunicateEvaluator, "calculate_reward", reward)
    result = evaluate_simulation(
        simulation,
        task,
        EvaluationType.COMMUNICATE,
        False,
        "mock",
        communicate_scoring=CommunicateScoring(scorer="substring"),
    )
    assert seen and result.reward == 1
    assert result.reward_breakdown[RewardType.COMMUNICATE] == 1
    assert (
        simulation.info["evaluation_log"]["missing_requests"] == [2]
        and result.info["evidence"] == "retained"
    )
    invalid = deepcopy(summary)
    invalid["stages"][1]["event_id"] = "wrong"
    with pytest.raises(ValueError, match="identities"):
        validate_delivery(task, invalid)
    invalid = deepcopy(summary)
    invalid["stages"][0]["completed"] = None
    with pytest.raises(ValueError, match="order"):
        validate_delivery(task, invalid)


@pytest.mark.parametrize(
    "ending", [TerminationReason.MAX_STEPS, TerminationReason.TIMEOUT]
)
def test_runner_budget_ending_marks_completed_after_saving_trace(monkeypatch, ending):
    from types import SimpleNamespace

    from tau2.runner.simulation import run_simulation

    task = Task(id="mock", user_scenario=UserScenario(instructions="do"))
    sim = SimulationRun(
        id="budget",
        task_id=task.id,
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason=ending,
        messages=[],
    )
    orch = SimpleNamespace(
        call_context=None,
        task=task,
        run=lambda: sim,
        agent=SimpleNamespace(),
        user=SimpleNamespace(),
        environment=SimpleNamespace(
            get_policy=lambda: "policy", get_domain_name=lambda: "mock"
        ),
    )
    saved = []

    def grade(**kw):
        assert saved and saved[0].evaluation_status == "pending"
        assert saved[0].info["grading_version"] == GRADING_VERSION
        return RewardInfo(reward=1)

    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", grade)
    result = run_simulation(
        orch, on_trajectory_ready=lambda s: saved.append(s.model_copy(deep=True))
    )
    assert (
        result.evaluation_status == "completed" and result.termination_reason == ending
    )
    assert result.reward_info.reward == 1

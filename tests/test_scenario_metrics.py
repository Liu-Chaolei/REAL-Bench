"""Simulator failures must not change measured agent task accuracy."""

import pytest

from tau2.data_model.simulation import (
    Info,
    Results,
    RewardInfo,
    SimulationRun,
    TerminationReason,
    UserInfo,
)
from tau2.data_model.tasks import EvaluationCriteria, Task, UserScenario
from tau2.environment.environment import EnvironmentInfo
from tau2.metrics.agent_metrics import compute_metrics


@pytest.mark.parametrize("invalid_reward", [0.0, 1.0])
def test_simulator_errors_are_excluded_and_agent_failures_still_count(invalid_reward):
    reasons = [
        TerminationReason.USER_STOP,
        TerminationReason.AGENT_ERROR,
        TerminationReason.USER_ERROR,
        TerminationReason.INFRASTRUCTURE_ERROR,
    ]
    simulations = [
        SimulationRun(
            id=str(i),
            task_id=str(i),
            trial=0,
            start_time="2026-01-01T00:00:00",
            end_time="2026-01-01T00:01:00",
            duration=60,
            termination_reason=reason,
            messages=[],
            agent_cost=0,
            reward_info=RewardInfo(
                reward=[1.0, 0.0, invalid_reward, invalid_reward][i]
            ),
        )
        for i, reason in enumerate(reasons)
    ]
    results = Results(
        info=Info(
            git_commit="fixture",
            num_trials=1,
            max_steps=100,
            max_errors=10,
            user_info=UserInfo(implementation="user_simulator"),
            agent_info={"implementation": "llm_agent"},
            environment_info=EnvironmentInfo(domain_name="mock", policy="test"),
        ),
        tasks=[
            Task(
                id=str(i),
                user_scenario=UserScenario(instructions="test"),
                evaluation_criteria=EvaluationCriteria(actions=[]),
            )
            for i in range(4)
        ],
        simulations=simulations,
    )
    metrics = compute_metrics(results)
    assert metrics.avg_reward == metrics.pass_hat_ks[1] == 0.5
    assert metrics.total_simulations == metrics.total_tasks == 2
    assert metrics.termination_error == 1
    assert metrics.user_simulator_error_count == metrics.infra_error_count == 1
    results.simulations = simulations[2:]
    metrics = compute_metrics(results)
    assert metrics.total_simulations == 0
    assert metrics.pass_hat_ks == {}
    assert metrics.user_simulator_error_count == metrics.infra_error_count == 1

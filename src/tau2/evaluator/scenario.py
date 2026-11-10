"""Shared environment assertions over a fully replayed scenario execution."""

from collections import Counter
from copy import deepcopy

from tau2.data_model.simulation import EnvAssertionCheck, RewardInfo
from tau2.data_model.tasks import RewardType
from tau2.environment.replay import replay_environment


def calculate_scenario_reward(
    environment_constructor,
    task,
    records,
    *,
    env_kwargs=None,
    strict=True,
    compare_tool_results=True,
    solo_mode=False,
):
    """Use ordinary environment assertion functions and reward composition."""
    environment = environment_constructor(
        solo_mode=solo_mode, **deepcopy(env_kwargs or {})
    )
    try:
        if records is None:
            raise ValueError("Timed tasks require their execution journal for scoring")
        replay_warnings = []
        replay_environment(
            environment,
            records,
            strict=strict,
            compare_tool_results=compare_tool_results,
            replay_warnings=replay_warnings,
        )
        checks = []
        for assertion in task.evaluation_criteria.env_assertions or []:
            met = environment.run_env_assertion(assertion, raise_assertion_error=False)
            checks.append(
                EnvAssertionCheck(env_assertion=assertion, met=met, reward=float(met))
            )
        reward = float(all(check.met for check in checks))
        basis = task.evaluation_criteria.reward_basis
        breakdown = (
            {RewardType.ENV_ASSERTION: reward}
            if RewardType.ENV_ASSERTION in basis
            else {}
        )
        if RewardType.DB in basis:
            raise ValueError(
                "Timed DB rewards require an explicit business-state assertion"
            )
        warning_counts = Counter(row["tool"] for row in replay_warnings)
        return RewardInfo(
            reward=reward if breakdown else 1.0,
            env_assertions=checks,
            reward_basis=basis,
            reward_breakdown=breakdown,
            info={
                "scenario_version": task.scenario.version,
                "replay_warnings": [
                    {"tool": tool, "count": count}
                    for tool, count in sorted(warning_counts.items())
                ],
                "reference_bindings": getattr(
                    environment.tools, "reference_bindings", {}
                ),
                "environment_assertions": getattr(
                    environment.tools, "goal_diagnostics", None
                ),
            },
        )
    finally:
        environment.close()

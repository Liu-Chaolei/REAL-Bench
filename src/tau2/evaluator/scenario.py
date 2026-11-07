"""Shared environment assertions over a fully replayed scenario execution."""

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
    solo_mode=False,
):
    """Use ordinary environment assertion functions and reward composition."""
    environment = environment_constructor(
        solo_mode=solo_mode, **deepcopy(env_kwargs or {})
    )
    try:
        if records is None:
            raise ValueError("Timed tasks require their execution journal for scoring")
        replay_environment(environment, records, strict=strict)
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
        return RewardInfo(
            reward=reward if breakdown else 1.0,
            env_assertions=checks,
            reward_basis=basis,
            reward_breakdown=breakdown,
            info={
                "scenario_version": task.scenario.version,
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

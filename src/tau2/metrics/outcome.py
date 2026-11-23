"""Explicit environment results for presentation, separate from task rewards."""

from tau2.data_model.tasks import RewardType


def is_gaia2_result(simulation, domain=None):
    """Identify Gaia2 through recorded provenance, never through a task-ID pattern."""
    info = simulation.info or {}
    reward = simulation.reward_info
    return (
        domain in {"gaia2", "gaia2_hidden"}
        or info.get("evaluation_log", {}).get("domain") == "gaia2"
        or bool(
            reward
            and any(
                c.env_assertion.func_name == "assert_scenario_goal"
                for c in reward.env_assertions or []
            )
        )
        or bool(
            reward
            and (reward.info or {}).get("scenario_version", "").startswith("gaia2-")
        )
    )


def env_assertion_value(simulation):
    """Read only the recorded environment component; no fallback to a total."""
    reward = simulation.reward_info
    if reward is None or simulation.evaluation_status in {
        "pending",
        "failed",
        "skipped",
    }:
        return None
    return (reward.reward_breakdown or {}).get(RewardType.ENV_ASSERTION)


def result_metric(simulation, domain=None):
    """Return the one single-trial performance value appropriate to this domain."""
    if is_gaia2_result(simulation, domain):
        return "ENV_ASSERTION", env_assertion_value(simulation)
    return "Reward", simulation.reward_info.reward if simulation.reward_info else None

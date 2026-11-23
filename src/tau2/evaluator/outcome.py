"""Shared outcome grading policy for live runs and trajectory re-evaluation."""

from tau2.data_model.simulation import TerminationReason
from tau2.persistent_tasks.scores import completion_score, consolidate_score

GRADING_VERSION = "tau-outcome-grading-v4"
GRADABLE_ENDINGS = frozenset(
    {
        TerminationReason.AGENT_STOP,
        TerminationReason.USER_STOP,
        TerminationReason.MAX_STEPS,
        TerminationReason.TIMEOUT,
    }
)


def can_grade(termination_reason):
    """Budget exhaustion with a returned trajectory permits outcome grading."""
    return termination_reason in GRADABLE_ENDINGS


def stamp_grading(simulation):
    """Record the scoring protocol without replacing execution provenance."""
    simulation.info = {**(simulation.info or {}), "grading_version": GRADING_VERSION}


def finalize_reward(simulation, reward, series=None, missing=()):
    """Attach grading metadata; delivery is recorded only in the evaluation log."""
    persistent_summary = (simulation.info or {}).get("persistent_tasks")
    if persistent_summary is not None:
        from tau2.data_model.tasks import RewardType

        persistent = persistent_summary.get("grading")
        if persistent is None:
            persistent = {"status": "ungraded", "completion_score": None}
        combine = persistent_summary.get("config", {}).get("combine_reward", False)
        if combine and RewardType.PERSISTENT_TASK in (reward.reward_basis or []):
            factor = completion_score(persistent)
            if factor is None:
                raise ValueError("Persistent reward requires complete graded evidence")
            reward.reward *= factor
            reward.reward_breakdown = {
                **(reward.reward_breakdown or {}),
                RewardType.PERSISTENT_TASK: factor,
            }
        reward.info = {
            **(reward.info or {}),
            "persistent_tasks": consolidate_score(persistent),
        }
    reward.info = {
        **(reward.info or {}),
        "grading_version": GRADING_VERSION,
        "termination_reason": simulation.termination_reason.value,
    }
    if series is not None:
        simulation.info = {
            **(simulation.info or {}),
            "evaluation_log": {
                **(simulation.info or {}).get("evaluation_log", {}),
                "scenario_series": series,
                "missing_requests": list(missing),
            },
        }
    return reward

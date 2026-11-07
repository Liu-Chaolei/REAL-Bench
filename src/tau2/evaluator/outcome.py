"""Shared outcome grading policy for live runs and trajectory re-evaluation."""

from tau2.data_model.simulation import TerminationReason

GRADING_VERSION = "tau-outcome-grading-v2"
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
    """Attach diagnostics and enforce delivery after component checks finish."""
    reward.info = {
        **(reward.info or {}),
        "grading_version": GRADING_VERSION,
        "termination_reason": simulation.termination_reason.value,
    }
    if series is not None:
        reward.info.update(series_version=series["version"], scenario_series=series)
        reward.info["missing_requests"] = list(missing)
    if missing:
        reward.reward = 0.0
        reward.info["note"] = "Not all continuous requests were delivered"
    return reward

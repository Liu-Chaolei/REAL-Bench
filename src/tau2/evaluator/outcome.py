"""Shared outcome grading policy for live runs and trajectory re-evaluation."""

from tau2.data_model.simulation import TerminationReason

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

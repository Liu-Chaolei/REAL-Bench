"""Provider-neutral state inspection of canonical operation results."""

from tau2.persistent_tasks.core.models import CanonicalState


def result_state(result):
    """Read canonical state without provider field aliases."""
    try:
        return CanonicalState(result.get("state"))
    except (AttributeError, TypeError, ValueError):
        return CanonicalState.UNKNOWN

"""Historical Qwen v4 result semantics, used only by frozen evidence grading."""

from tau2.persistent_tasks.core.models import CanonicalState


def result_state(result):
    """Read the original result shape without changing historical artifacts."""
    from tau2.persistent_tasks.adapters.qwen import QWEN_STATES

    status = result.get("task_status") if isinstance(result, dict) else None
    return QWEN_STATES.get(status, CanonicalState.UNKNOWN)

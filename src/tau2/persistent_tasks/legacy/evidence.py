"""Read-only adaptation of pre-canonical observation fields for replay."""

from dataclasses import replace


def legacy_observation(observation):
    """Restore historical projector/binder semantics without changing old hashes."""
    from tau2.persistent_tasks.adapters.qwen import QWEN_STATES

    raw = observation.raw or {}
    state = QWEN_STATES.get(observation.status)
    return replace(
        observation,
        objective=(raw.get("task") or {}).get("objective"),
        status=None
        if observation.status == "cancelling"
        else (state.value if state else observation.status),
    )

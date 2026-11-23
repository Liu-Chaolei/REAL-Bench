"""Resolve private task configuration and check async execution prerequisites."""

from __future__ import annotations


class AsyncTaskUnavailable(ValueError):
    """A task or Agent cannot support this measurement before model startup."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


def resolve_config(task, explicit=None):
    """Use embedded configuration, rejecting an inconsistent external override."""
    from tau2.persistent_tasks.config import PersistentTaskConfig

    embedded = task.scenario.async_interaction if task.scenario else None
    if embedded and embedded.get("version") == "gaia2-interaction-variant-v1":
        if explicit is not None:
            raise ValueError("Interaction variants use the active metric runtime")
        return None
    if embedded is not None and explicit is not None:
        left = PersistentTaskConfig.model_validate(embedded)
        right = PersistentTaskConfig.model_validate(explicit)
        if left.digest() != right.digest():
            raise ValueError("Embedded and external async configurations conflict")
    return embedded if embedded is not None else explicit


def check_task(task, profile):
    """Check data readiness independently of provider capabilities."""
    profile.validate_task(task)
    if task.scenario and task.scenario.async_interaction is not None:
        metadata = task.scenario.async_metadata or {}
        if metadata.get("readiness") != "ready":
            raise AsyncTaskUnavailable(
                "task_not_ready",
                "; ".join(metadata.get("reasons", [])) or "Async task is a draft",
            )
    if (
        any(r.operation in {"modify", "cancel"} for r in profile.requests)
        and not profile.overlay
    ):
        raise AsyncTaskUnavailable(
            "task_not_ready", "Mutation requires a business overlay"
        )


def evidence_source(agent):
    """Return an optional public evidence source without inspecting provider internals."""
    getter = getattr(agent, "get_async_evidence_source", None)
    return getter() if getter else None


def check_capabilities(profile, source):
    """Fail unsupported tasks before connecting model sessions."""
    if source is None:
        raise AsyncTaskUnavailable("unsupported", "Agent has no async evidence Adapter")
    capabilities = source.get_capabilities()
    operations = set(capabilities.get("operations", []))
    states = set(capabilities.get("states", []))
    required = {r.operation for r in profile.requests}
    missing = required - operations
    missing_states = {r.state.value for r in profile.requests} - states
    if (
        missing
        or missing_states
        or not capabilities.get("generated_reply")
        or not capabilities.get("task_identity")
    ):
        raise AsyncTaskUnavailable(
            "unsupported",
            f"Missing async capabilities: operations={sorted(missing)}, states={sorted(missing_states)}; task_identity/generated_reply required",
        )
    for operation, capability in (
        ("modify", "update_confirmation"),
        ("cancel", "cancel_confirmation"),
    ):
        if operation in required and not capabilities.get(capability):
            raise AsyncTaskUnavailable(
                "unsupported", f"Missing async capability: {capability}"
            )
    if len(profile.bindings) > 1 and not capabilities.get("multi_task"):
        raise AsyncTaskUnavailable(
            "unsupported", "Multi-task observation is unsupported"
        )


def skip_unavailable_async(function):
    """Preserve per-task skip records and let a mixed batch continue."""
    from functools import wraps

    @wraps(function)
    def wrapped(config, task, **kwargs):
        try:
            return function(config, task, **kwargs)
        except AsyncTaskUnavailable as error:
            import uuid

            from tau2.data_model.simulation import SimulationRun, TerminationReason
            from tau2.utils.utils import get_now

            now = get_now()
            return SimulationRun(
                id=str(uuid.uuid4()),
                task_id=task.id,
                seed=kwargs.get("seed") or 0,
                trial=kwargs.get("trial"),
                start_time=now,
                end_time=now,
                duration=0,
                messages=[],
                evaluation_status="skipped",
                reward_info=None,
                termination_reason=TerminationReason(error.status),
                agent_cost=0,
                user_cost=0,
                info={
                    "async_availability": {
                        "status": error.status,
                        "reason": str(error),
                        "reward": None,
                    }
                },
            )

    return wrapped

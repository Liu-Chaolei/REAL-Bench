"""Strict binding of native task IDs to benchmark roles."""

from __future__ import annotations

from typing import Any

from tau2.persistent_tasks.core.models import BackendTaskObservation, TaskIdentity


class TaskIdentityBinder:
    """Bind unique creation evidence using private stage activation rules."""

    def __init__(self, bindings: list[dict[str, Any]]):
        self.bindings = list(bindings)
        self.bound: dict[str, TaskIdentity] = {}
        self.active_stage: int | None = None
        self.active_since: float | None = None
        self.completed_events: set[str] = set()
        self.issues: list[dict] = []

    def bind_observation(
        self,
        observation: BackendTaskObservation,
        *,
        allowed_roles: set[str] | None = None,
    ) -> TaskIdentity | None:
        """Accept a real creation only when exactly one activated role matches."""
        key = observation.backend_task_id
        if key is None or observation.kind != "task_created":
            return None
        if key in self.bound:
            return self.bound[key]
        if observation.parent_task_id:
            return None
        if self.active_since is not None and observation.timestamp < self.active_since:
            return None
        matches = []
        for binding in self.bindings:
            if (
                allowed_roles is not None
                and binding.get("role", "primary") not in allowed_roles
            ):
                continue
            if self.active_stage != binding["source_stage"]:
                continue
            if (
                not set(binding.get("activation_event_ids", []))
                <= self.completed_events
            ):
                continue
            if binding.get("backend_task_id") not in (None, key):
                continue
            if observation.source_task_id not in (None, binding["source_task_id"]):
                continue
            if observation.source_stage not in (None, binding["source_stage"]):
                continue
            objective = observation.objective or ""
            if binding.get(
                "objective_prefix"
            ) and not objective.lstrip().casefold().startswith(
                binding["objective_prefix"].casefold()
            ):
                continue
            if (
                binding.get("objective_contains")
                and binding["objective_contains"].casefold() not in objective.casefold()
            ):
                continue
            role = binding.get("role", "primary")
            if observation.role not in (None, role):
                continue
            matches.append(
                TaskIdentity(
                    role, binding["source_task_id"], binding["source_stage"], key
                )
            )
        if len(matches) != 1:
            if matches:
                self.issues.append({"reason": "ambiguous_binding", "task_id": key})
            return None
        identity = matches[0]
        if any(
            (old.source_task_id, old.source_stage, old.role)
            == (identity.source_task_id, identity.source_stage, identity.role)
            for old in self.bound.values()
        ):
            self.issues.append({"reason": "multiple_tasks_for_role", "task_id": key})
            return None
        self.bound[key] = identity
        return identity

"""Ordered projection of native task lifecycle states."""

from __future__ import annotations

from typing import Any

from tau2.persistent_tasks.core.models import (
    BackendTaskObservation,
    CanonicalState,
    StateTransition,
    TaskIdentity,
)


class BackendTaskProjector:
    """Project only explicitly bound, ordered lifecycle evidence."""

    _STATUS_MAP = {state.value: state for state in CanonicalState}
    TERMINAL = {
        CanonicalState.COMPLETED,
        CanonicalState.FAILED,
        CanonicalState.CANCELLED,
    }

    def __init__(self):
        self.identities: dict[str, TaskIdentity] = {}
        self.states: dict[str, CanonicalState] = {}
        self.last: dict[str, tuple[int | None, float]] = {}
        self.seen: set[tuple[str | None, str]] = set()
        self.results: dict[str, Any] = {}
        self.pending_completion: set[str] = set()
        self.issues: list[dict] = []

    def bind(self, identity: TaskIdentity) -> None:
        """Bind a task without silently overwriting its identity."""
        previous = self.identities.get(identity.backend_task_id)
        if previous is not None and previous != identity:
            raise ValueError("backend task ID is already bound to another task")
        self.identities[identity.backend_task_id] = identity
        self.states.setdefault(identity.backend_task_id, CanonicalState.NOT_STARTED)

    def ingest(self, observation: BackendTaskObservation) -> StateTransition | None:
        """Ignore diagnostics and stale events; require actual completion results."""
        key = observation.backend_task_id
        if key not in self.identities:
            return None
        if observation.kind not in {
            "task_created",
            "task_state_changed",
            "task_result",
            "backend_task_state",
        }:
            return None
        if observation.event_id:
            token = (observation.session_id, observation.event_id)
            if token in self.seen:
                return None
            self.seen.add(token)
        if observation.status is None and not (
            observation.kind == "task_result" and key in self.pending_completion
        ):
            return None
        last_seq, last_time = self.last.get(key, (None, float("-inf")))
        if (
            last_seq is not None
            and observation.sequence is not None
            and observation.sequence <= last_seq
        ) or observation.timestamp < last_time:
            return None
        self.last[key] = (observation.sequence, observation.timestamp)
        status = (observation.status or "completed").casefold()
        current = self._STATUS_MAP.get(status, CanonicalState.UNKNOWN)
        previous = self.states[key]
        if previous in self.TERMINAL and current != previous:
            self.issues.append(
                {
                    "reason": "terminal_state_conflict",
                    "task_id": key,
                    "event_id": observation.event_id,
                }
            )
            return None
        if current == CanonicalState.COMPLETED and not observation.result_available:
            self.pending_completion.add(key)
            return None
        if current == CanonicalState.UNKNOWN:
            self.issues.append(
                {"reason": "unknown_task_status", "status": status, "task_id": key}
            )
        if observation.result_available:
            self.results[key] = observation.result
            self.pending_completion.discard(key)
        self.states[key] = current
        if current == previous:
            return None
        return StateTransition(
            self.identities[key], previous, current, observation.timestamp, observation
        )

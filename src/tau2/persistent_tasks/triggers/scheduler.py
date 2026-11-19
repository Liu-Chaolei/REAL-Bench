"""Generic state-triggered request scheduler."""

from __future__ import annotations

from tau2.persistent_tasks.core.models import (
    BackendRequest,
    StateTransition,
)
from tau2.persistent_tasks.core.projection import BackendTaskProjector


class BackendTriggerScheduler:
    """Release once, preserving condition, expiry and delivery evidence."""

    def __init__(self, requests: list[BackendRequest]):
        if len({r.request_id for r in requests}) != len(requests):
            raise ValueError("duplicate request IDs")
        self.requests = list(requests)
        self.states = {}
        self.transition_times = {}
        self.released: set[str] = set()
        self.rows = {
            r.request_id: {
                "status": "pending",
                "condition_met_at": None,
                "queued_at": None,
                "delivery_started_at": None,
                "delivered_at": None,
            }
            for r in requests
        }
        self.activated_at: dict[int, float] = {}

    @staticmethod
    def key(identity):
        """Keep multiple roles in one source stage independent."""
        return identity.source_task_id, identity.source_stage, identity.role

    def ingest(self, transition: StateTransition) -> None:
        """Update condition timing and expire requests whose state has passed."""
        key = self.key(transition.identity)
        self.states[key] = transition.current
        self.transition_times[key] = transition.timestamp
        for request in self.requests:
            if self.key(request) != key:
                continue
            row = self.rows[request.request_id]
            if row["status"] in {"queued", "delivered", "expired", "invalidated"}:
                continue
            if transition.current == request.state and row["condition_met_at"] is None:
                row.update(
                    status="condition_met", condition_met_at=transition.timestamp
                )
            elif (
                row["condition_met_at"] is not None
                and transition.current != request.state
            ):
                row.update(status="expired", reason="expired_state")
            elif (
                transition.current in BackendTaskProjector.TERMINAL
                and request.state not in BackendTaskProjector.TERMINAL
            ):
                row.update(status="invalidated", reason="terminal_before_condition")

    def release(self, *, timestamp: float, turn_boundary: bool) -> list[BackendRequest]:
        """Queue one request at most, preserving timeout and delay evidence."""
        released = []
        outstanding = any(row["status"] == "queued" for row in self.rows.values())
        for request in self.requests:
            row = self.rows[request.request_id]
            if row["status"] in {"delivered", "expired", "invalidated"}:
                continue
            activated = self.activated_at.get(request.source_stage)
            if (
                activated is not None
                and timestamp >= activated + request.max_wait_seconds
            ):
                row.update(status="expired", reason="request_timeout")
                continue
            if row["status"] == "queued" or outstanding:
                continue
            condition = row["condition_met_at"]
            if condition is None or self.states.get(self.key(request)) != request.state:
                continue
            if timestamp < condition + request.delay_seconds:
                continue
            if request.after_turn_boundary and not turn_boundary:
                continue
            row.update(status="queued", queued_at=timestamp)
            self.released.add(request.request_id)
            released.append(request)
            break
        return released

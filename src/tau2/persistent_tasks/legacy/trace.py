"""Trace collection kept independent from task state and scoring."""

from __future__ import annotations

from typing import Any

from tau2.persistent_tasks.models import TaskTraceRecord


class TaskTrace:
    """Collect normalized task operations and release timing records."""

    def __init__(self):
        self.operations: list[TaskTraceRecord] = []
        self.releases: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.bindings: list[dict[str, Any]] = []

    def operation(self, **kwargs) -> TaskTraceRecord:
        """Append and return an operation record."""
        record = TaskTraceRecord(**kwargs)
        self.operations.append(record)
        return record

    def release(
        self,
        request_id: str,
        *,
        condition_met_at: float,
        queued_at: float,
        delivered_at: float | None = None,
    ):
        """Record condition, queue and optional transport times."""
        self.releases.append(
            {
                "request_id": request_id,
                "condition_met_at": condition_met_at,
                "queued_at": queued_at,
                "delivered_at": delivered_at,
            }
        )

    def delivery(self, request_id: str, *, delivered_at: float) -> None:
        """Record the point at which a queued request reached the user path."""
        for row in reversed(self.releases):
            if row["request_id"] == request_id:
                row["delivered_at"] = delivered_at
                return

    def observation(self, event) -> None:
        """Record a provider-neutral observation for replay and diagnostics."""
        self.observations.append(event.model_dump(mode="json"))

    def binding(self, *, status: str, event_id: str | None, reason: str | None) -> None:
        """Record an event binding decision, including unmatched observations."""
        self.bindings.append({"status": status, "event_id": event_id, "reason": reason})

    def dump(self) -> dict[str, Any]:
        """Return JSON-compatible trace data."""
        return {
            "operations": [
                record.model_dump(mode="json") for record in self.operations
            ],
            "releases": list(self.releases),
            "observations": list(self.observations),
            "bindings": list(self.bindings),
        }

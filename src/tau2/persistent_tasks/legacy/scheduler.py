"""Deterministic state-event scheduler and request release logic."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from tau2.persistent_tasks.models import (
    PersistentTaskRequest,
    ReleaseCondition,
    TaskState,
    TaskStateEvent,
)
from tau2.persistent_tasks.projection import MilestoneProjector
from tau2.persistent_tasks.service import PersistentTaskService


@dataclass
class ReleasedRequest:
    """A request released after its condition became true."""

    request_id: str
    text: str
    condition_met_at: float
    queued_at: float


class PersistentTaskScheduler:
    """Advance stage-local task events and release user requests."""

    def __init__(
        self,
        service: PersistentTaskService,
        events: list[TaskStateEvent],
        requests: list[PersistentTaskRequest],
    ):
        self.service = service
        self.events = list(events)
        self.requests = list(requests)
        self.projector = MilestoneProjector(service, self.events)
        self.completed_events = self.projector.completed_events
        self.released: set[str] = set()
        self._event_times = self.projector.event_times
        self._eligible_at: dict[str, float] = {}
        self._lock = threading.RLock()

    def on_event_completed(
        self, event_id: str, *, timestamp: float
    ) -> list[ReleasedRequest]:
        """Bind an original GAIA2 event and advance dependent task events."""
        with self._lock:
            self.projector.observe(event_id, timestamp)
            return self.advance(elapsed=timestamp, turn_boundary=False)

    def on_turn_boundary(self, *, timestamp: float) -> list[ReleasedRequest]:
        """Release requests that became eligible during the preceding turn."""
        return self.advance(elapsed=timestamp, turn_boundary=True)

    def on_request_delivered(self, request_id: str, *, timestamp: float) -> None:
        """Record actual transport of a released request."""
        self.service.trace.delivery(request_id, delivered_at=timestamp)

    def advance(
        self, *, elapsed: float, turn_boundary: bool = True
    ) -> list[ReleasedRequest]:
        """Apply due events and return requests newly eligible at this time."""
        with self._lock:
            return self._advance(elapsed=elapsed, turn_boundary=turn_boundary)

    def _advance(self, *, elapsed: float, turn_boundary: bool) -> list[ReleasedRequest]:
        applied = self.projector.advance(elapsed)
        for event in applied:
            task = next(
                task
                for task in self.service.snapshot()
                if task.source_task_id == event.source_task_id
                and task.source_stage == event.source_stage
            )
            if task.state in {TaskState.CANCELLED, TaskState.CANCEL_REQUESTED}:
                self._invalidate_for_task(task.source_task_id, task.source_stage)

        result = []
        for request in self.requests:
            if request.request_id in self.released:
                continue
            ready_at = self._condition_ready_at(request.release_condition, elapsed)
            if ready_at is not None and elapsed >= ready_at:
                condition_time = self._eligible_at.setdefault(
                    request.request_id, ready_at
                )
                if request.release_condition.after_turn_boundary and not turn_boundary:
                    continue
                self.released.add(request.request_id)
                self.service.trace.release(
                    request.request_id,
                    condition_met_at=condition_time,
                    queued_at=elapsed,
                )
                result.append(
                    ReleasedRequest(
                        request_id=request.request_id,
                        text=request.text,
                        condition_met_at=condition_time,
                        queued_at=elapsed,
                    )
                )
        return result

    def _condition_ready_at(
        self, condition: ReleaseCondition, elapsed: float
    ) -> float | None:
        """Return when a release condition, including its delay, becomes ready."""
        if condition.event_id:
            if condition.event_id not in self.completed_events:
                return None
            base = self._event_times[condition.event_id]
            return base + condition.delay_seconds

        if condition.source_task_id:
            task = next(
                (
                    task
                    for task in self.service.snapshot()
                    if task.source_task_id == condition.source_task_id
                    and task.source_stage == condition.source_stage
                ),
                None,
            )
            if task is None:
                return None
            if condition.state is not None and task.state != condition.state:
                return None
            if (
                condition.milestone
                and condition.milestone not in task.completed_milestones
            ):
                return None

            base = None
            for event in self.events:
                if (
                    event.source_task_id != condition.source_task_id
                    or event.source_stage != condition.source_stage
                    or event.id not in self.completed_events
                ):
                    continue
                if condition.milestone and event.milestone_id == condition.milestone:
                    base = self._event_times[event.id]
                    break
                if condition.state is not None and event.new_state == condition.state:
                    base = max(
                        base or self._event_times[event.id], self._event_times[event.id]
                    )
            if base is None:
                base = elapsed
            return base + condition.delay_seconds
        return elapsed + condition.delay_seconds

    def _invalidate_for_task(self, source_task_id: str, source_stage: int) -> None:
        """Prevent future result releases after cancellation."""
        for request in self.requests:
            if (
                request.release_condition.source_task_id == source_task_id
                and request.release_condition.source_stage == source_stage
                and request.request_id not in self.released
            ):
                if request.release_condition.state == TaskState.COMPLETED:
                    self.released.add(request.request_id)

"""Runtime coordinator shared by native, replay and provider adapters."""

from __future__ import annotations

from typing import Any, Callable

from tau2.persistent_tasks.event_binding import EventBinder
from tau2.persistent_tasks.models import ObservedEvent
from tau2.persistent_tasks.scheduler import PersistentTaskScheduler, ReleasedRequest
from tau2.persistent_tasks.trace import TaskTrace


class PersistentTaskRuntime:
    """Join observation, binding, scheduling and request delivery."""

    def __init__(
        self,
        *,
        events,
        scheduler: PersistentTaskScheduler,
        scenario_id: str | None = None,
        expected_checks: list[dict] | None = None,
        expected_operations: list[dict] | None = None,
        trace: TaskTrace | None = None,
        enqueue: Callable[[str, str], None] | None = None,
        normalize_arguments: Callable[[str, dict], dict] | None = None,
    ):
        self.scheduler = scheduler
        self.scenario_id = scenario_id
        self.expected_checks = expected_checks or []
        self.expected_operations = expected_operations or []
        self.pending_requests: set[str] = set()
        self.active_request: str | None = None
        self.replies: dict[str, str] = {}
        self.trace = trace or scheduler.service.trace
        self.enqueue = enqueue
        self.binder = EventBinder(
            events,
            normalize_arguments=normalize_arguments,
            on_bound=self._on_bound,
        )

    def observe(self, event: ObservedEvent) -> str:
        """Record and bind a provider observation."""
        self.trace.observation(event)
        result = self.binder.bind(event)
        self.trace.binding(
            status=result.status, event_id=result.event_id, reason=result.reason
        )
        return result.status

    def source_event_completed(self, event_id: str, result: Any, timestamp: float):
        """Accept a native ScenarioRuntime completion without re-matching it."""
        event = next((item for item in self.binder.events if item.id == event_id), None)
        observed = ObservedEvent(
            provider="tau",
            kind="tool_call_completed",
            timestamp=timestamp,
            event_id=event_id,
            call_id=event_id,
            tool_name=getattr(event, "tool", None),
            arguments=getattr(event, "arguments", {}) if event else {},
            result=result,
            accepted=True,
        )
        self.trace.observation(observed)
        self.binder.completed[event_id] = timestamp
        self.binder.results[event_id] = result
        return self._on_bound(event_id, result, timestamp)

    def _on_bound(self, event_id: str, result: Any, timestamp: float):
        released = self.scheduler.on_event_completed(event_id, timestamp=timestamp)
        return self._enqueue(released)

    def on_turn_boundary(self, *, timestamp: float) -> list[ReleasedRequest]:
        """Release eligible requests at an explicit communication boundary."""
        return self._enqueue(self.scheduler.on_turn_boundary(timestamp=timestamp))

    def observe_delivery(
        self, role: str, text: str, event_id: str | None, timestamp: float
    ) -> None:
        """Associate actual delivered speech with a released request."""
        if role == "user" and event_id in self.pending_requests:
            self.pending_requests.remove(event_id)
            self.active_request = event_id
            self.scheduler.service.set_active_request(event_id)
            self.mark_delivered(event_id, timestamp=timestamp)
        elif role == "agent" and self.active_request is not None:
            previous = self.replies.get(self.active_request, "")
            self.replies[self.active_request] = (previous + " " + text).strip()

    def mark_delivered(self, request_id: str, *, timestamp: float) -> None:
        self.scheduler.on_request_delivered(request_id, timestamp=timestamp)

    def _enqueue(self, released: list[ReleasedRequest]) -> list[ReleasedRequest]:
        if self.enqueue is not None:
            for request in released:
                self.pending_requests.add(request.request_id)
                self.enqueue(request.request_id, request.text)
        return released

    def summary(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "expected_checks": self.expected_checks,
            "expected_operations": self.expected_operations,
            "replies": dict(self.replies),
            "trace": self.trace.dump(),
            "binding": self.binder.snapshot(),
            "projection": [
                task.model_dump(mode="json")
                for task in self.scheduler.service.snapshot()
            ],
        }

"""Deterministic milestone projection for native task stages."""

from __future__ import annotations

from tau2.persistent_tasks.models import TaskStateEvent
from tau2.persistent_tasks.service import PersistentTaskService


class MilestoneProjector:
    """Apply declared state events after their native dependencies complete."""

    def __init__(self, service: PersistentTaskService, events: list[TaskStateEvent]):
        self.service = service
        self.events = list(events)
        self.completed_events: set[str] = set()
        self.event_times: dict[str, float] = {}

    def observe(self, event_id: str, timestamp: float) -> list[TaskStateEvent]:
        """Record a native event and apply every due dependent milestone."""

        if event_id in self.completed_events:
            return []
        self.completed_events.add(event_id)
        self.event_times[event_id] = timestamp
        return self.advance(timestamp)

    def advance(self, timestamp: float) -> list[TaskStateEvent]:
        applied = []
        changed = True
        while changed:
            changed = False
            for event in self.events:
                if event.id in self.completed_events:
                    continue
                if not set(event.dependencies) <= self.completed_events:
                    continue
                dependency_time = max(
                    (
                        self.event_times.get(parent, 0.0)
                        for parent in event.dependencies
                    ),
                    default=0.0,
                )
                if timestamp < dependency_time + event.delay_seconds:
                    continue
                self.service.apply_event(
                    event, now=timestamp, completed_events=self.completed_events
                )
                self.completed_events.add(event.id)
                self.event_times[event.id] = timestamp
                applied.append(event)
                changed = True
        return applied

    def snapshot(self) -> dict:
        return {
            "completed_events": sorted(self.completed_events),
            "event_times": dict(self.event_times),
            "tasks": [task.model_dump(mode="json") for task in self.service.snapshot()],
        }

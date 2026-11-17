"""OpenAI Realtime/GPT Live event adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.core.observation import event_from_mapping
from tau2.persistent_tasks.models import ObservedEvent


class GPTLiveEventAdapter:
    """Translate response and function-call events without judging progress."""

    provider = "gpt_live"

    def convert(self, payload: Mapping[str, Any]) -> ObservedEvent:
        value = dict(payload)
        event_type = value.get("type") or value.get("event")
        if event_type in {
            "response.function_call_arguments.done",
            "function_call_output",
        }:
            value["kind"] = "tool_call_completed"
            value.setdefault("accepted", True)
        elif event_type in {"response.failed", "response.incomplete"}:
            value["kind"] = "tool_call_failed"
            value.setdefault("accepted", False)
        elif event_type in {"response.done", "response.completed"}:
            value["kind"] = "backend_update"
        return event_from_mapping(value, provider=self.provider)

    def events(self, payloads: Iterable[Mapping[str, Any]]) -> list[ObservedEvent]:
        return [self.convert(payload) for payload in payloads]

    def backend_observation(self, payload: Mapping[str, Any]) -> BackendTaskObservation:
        """Convert a backend task update; response completion is only evidence."""
        value = dict(payload)
        return BackendTaskObservation(
            provider=self.provider,
            kind=value.get("kind") or value.get("type") or "backend_update",
            timestamp=float(value.get("timestamp", value.get("time", 0))),
            backend_task_id=value.get("task_id") or value.get("backend_task_id"),
            request_id=value.get("request_id"),
            source_task_id=value.get("source_task_id"),
            source_stage=value.get("source_stage"),
            status=value.get("status") or value.get("task_status"),
            operation=value.get("operation"),
            arguments=value.get("arguments") or {},
            result=value.get("result"),
            raw=value,
        )

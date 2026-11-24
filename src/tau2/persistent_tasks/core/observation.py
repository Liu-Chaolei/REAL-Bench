"""Provider-neutral observation events and small adapter helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tau2.persistent_tasks.core.models import ObservedEvent

TOOL_COMPLETED = "tool_call_completed"
TOOL_STARTED = "tool_call_started"
TOOL_FAILED = "tool_call_failed"
TURN_COMPLETED = "agent_turn_completed"


def event_from_mapping(
    value: Mapping[str, Any], *, provider: str, default_timestamp: float = 0.0
) -> ObservedEvent:
    """Normalize a backend event mapping without interpreting its business meaning."""

    kind = value.get("kind") or value.get("type") or value.get("event")
    if not kind:
        raise ValueError("observed event has no kind/type")
    status = value.get("status")
    if kind in {"tool_call", "function_call", "function_call_output"}:
        kind = (
            TOOL_COMPLETED
            if status in {None, "completed", "succeeded", "success"}
            else TOOL_FAILED
        )
    return ObservedEvent(
        provider=provider,
        kind=kind,
        timestamp=float(value.get("timestamp", value.get("time", default_timestamp))),
        event_id=value.get("event_id") or value.get("id"),
        call_id=value.get("call_id") or value.get("tool_call_id"),
        channel=value.get("channel") or value.get("direction"),
        tool_name=value.get("tool_name") or value.get("name") or value.get("function"),
        arguments=value.get("arguments") or value.get("args") or {},
        result=value.get("result", value.get("output")),
        accepted=value.get("accepted"),
        raw=value,
    )


def normalize_events(
    values: Iterable[Mapping[str, Any]], *, provider: str
) -> list[ObservedEvent]:
    """Convert a batch of backend payloads into deterministic event objects."""

    return [event_from_mapping(value, provider=provider) for value in values]

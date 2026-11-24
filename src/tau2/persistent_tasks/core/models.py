"""Provider-neutral persistent-task contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CanonicalState(StrEnum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendTaskObservation:
    """One native event; timestamp is normalized by the receiving runtime."""

    provider: str
    kind: str
    timestamp: float
    backend_task_id: str | None = None
    request_id: str | None = None
    source_task_id: str | None = None
    source_stage: int | None = None
    status: str | None = None
    operation: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any = None
    raw: Any = None
    event_id: str | None = None
    sequence: int | None = None
    session_id: str | None = None
    call_id: str | None = None
    parent_task_id: str | None = None
    success: bool | None = None
    result_available: bool = False
    received_monotonic: float | None = None
    provider_timestamp: float | None = None
    role: str | None = None
    objective: str | None = None
    effect: str | None = None
    purpose: str | None = None
    response_id: str | None = None
    turn_id: str | None = None
    text: str | None = None
    final: bool = False
    confirmed_updates: list[dict[str, Any]] | None = None
    text_is_snapshot: bool = False

    def __post_init__(self):
        if not isfinite(self.timestamp):
            raise ValueError("backend timestamp must be finite")

    def dump(self):
        """Return the complete serializable event, including original evidence."""
        return asdict(self)


class ObservedEvent(BaseModel):
    """A provider-neutral event consumed by binding and scheduling."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    kind: str
    timestamp: float
    event_id: str | None = None
    call_id: str | None = None
    channel: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    accepted: bool | None = None
    raw: Any = None

    @model_validator(mode="after")
    def valid_event(self):
        if self.kind == "tool_call_completed" and not self.tool_name:
            raise ValueError("completed tool events require tool_name")
        return self

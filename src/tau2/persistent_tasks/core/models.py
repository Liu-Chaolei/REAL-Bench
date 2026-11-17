"""Provider-neutral persistent-task contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from tau2.config import DEFAULT_PERSISTENT_TASK_MAX_WAIT_SECONDS


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


@dataclass(frozen=True)
class TaskIdentity:
    """Benchmark role bound to one real backend task."""

    role: str
    source_task_id: str
    source_stage: int
    backend_task_id: str


@dataclass(frozen=True)
class StateTransition:
    identity: TaskIdentity
    previous: CanonicalState
    current: CanonicalState
    timestamp: float
    observation: BackendTaskObservation


@dataclass(frozen=True)
class BackendRequest:
    """A private request released from a declared state transition."""

    request_id: str
    text: str
    source_task_id: str
    source_stage: int
    state: CanonicalState
    delay_seconds: float = 0.0
    after_turn_boundary: bool = True
    role: str = "primary"
    max_wait_seconds: float = DEFAULT_PERSISTENT_TASK_MAX_WAIT_SECONDS
    expected_operation: str = "query"
    reply_expectation: str = "Accurately report the native task status and result."
    expected_arguments: dict[str, Any] | None = None
    request_operation_policy: str = "post_delivery_only"
    prefetch_max_age_seconds: float = 120.0

"""Small, serializable models for persistent task benchmark scenarios."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class TaskState(StrEnum):
    """Provider-neutral lifecycle used by persistent-task projections."""

    WAITING = "waiting"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TaskProgress(BaseModel):
    """Progress exposed to an agent and checked by the evaluator."""

    model_config = ConfigDict(extra="forbid")

    current_step: str
    completed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.completed_steps > self.total_steps:
            raise ValueError("completed_steps cannot exceed total_steps")
        return self


class TaskResult(BaseModel):
    """A completed task result."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    output: Any = None


class PersistentTask(BaseModel):
    """A projection of one original Gaia2 task stage."""

    model_config = ConfigDict(extra="forbid")

    source_task_id: str
    source_stage: int = Field(ge=0)
    title: str
    summary: str
    state: TaskState
    progress: TaskProgress
    completed_milestones: set[str] = Field(default_factory=set)
    result: TaskResult | None = None
    mutable_fields: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def state_result_consistency(self):
        if self.state == TaskState.COMPLETED and self.result is None:
            raise ValueError("completed tasks require a result")
        if self.state != TaskState.COMPLETED and self.result is not None:
            raise ValueError("only completed tasks may carry a result")
        return self


class TaskStateEvent(BaseModel):
    """A deterministic background transition in one stage."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source_task_id: str
    source_stage: int = Field(ge=0)
    new_state: TaskState
    milestone_id: str | None = None
    milestone_description: str | None = None
    delay_seconds: float = Field(default=0, ge=0)
    dependencies: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("dependencies", "depends_on"),
    )
    progress: TaskProgress | None = None
    result: TaskResult | None = None


class TaskMilestone(BaseModel):
    """A business milestone derived from original GAIA2 events."""

    model_config = ConfigDict(extra="forbid")

    id: str
    after_events: list[str] = Field(min_length=1)
    state: TaskState
    description: str


class ReleaseCondition(BaseModel):
    """A small, data-only condition for releasing a user request."""

    model_config = ConfigDict(extra="forbid")

    source_task_id: str | None = None
    source_stage: int | None = Field(default=None, ge=0)
    state: TaskState | None = None
    milestone: str | None = None
    event_id: str | None = None
    delay_seconds: float = Field(default=0, ge=0)
    after_turn_boundary: bool = True

    @model_validator(mode="after")
    def has_trigger(self):
        if not self.source_task_id and not self.event_id:
            raise ValueError("a release condition needs source_task_id or event_id")
        if self.source_task_id and self.source_stage is None:
            raise ValueError("source_task_id requires source_stage")
        if self.event_id and (self.state or self.milestone is not None):
            raise ValueError("event_id cannot be combined with task state fields")
        if self.milestone and not self.source_task_id:
            raise ValueError("milestone conditions require source_task_id")
        return self


class PersistentTaskRequest(BaseModel):
    """A request released to the user simulator."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    text: str
    release_condition: ReleaseCondition


class TaskOperation(BaseModel):
    """Normalized operation emitted by an adapter."""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    operation: Literal["list", "query", "modify", "cancel", "result"]
    source_task_id: str | None = None
    source_stage: int | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class TaskTraceRecord(BaseModel):
    """One immutable state-changing or read operation record."""

    model_config = ConfigDict(extra="forbid")

    request_id: str | None = None
    operation: str
    source_task_id: str | None = None
    source_stage: int | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    state_before: TaskState | None = None
    state_after: TaskState | None = None
    result: Any = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None


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

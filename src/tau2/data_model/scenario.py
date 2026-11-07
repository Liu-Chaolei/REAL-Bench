"""Optional, domain-independent contracts for stateful timed tasks."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class ScenarioAction(BaseModel):
    """A private scenario operation, anchor, or scripted user utterance."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["reference", "environment", "user"]
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    delay_seconds: float = Field(default=0, ge=0)
    time_comparator: str | None = None
    channel: Literal["tool", "agent"] = "tool"
    match_fields: list[str] | None = None
    semantic_fields: list[str] = Field(default_factory=list)


class ScenarioStage(BaseModel):
    """Private question/answer data for one part of a continuous task.

    Event definitions live once in the scenario graph. This index retains the
    evidence and outcome checks without carrying generation or acceptance logs.
    It does not by itself implement live stage delivery or stage-aware grading.
    """

    model_config = ConfigDict(extra="forbid")

    stage: int = Field(ge=0)
    request: str
    event_ids: list[str]
    relation: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    criteria: dict[str, Any]


class ScenarioSpec(BaseModel):
    """Private execution data; never render this object into an actor prompt."""

    model_config = ConfigDict(extra="forbid")

    version: str = "scenario-v1"
    start_time: float
    timezone: str = "UTC"
    source_id: str
    source_sha256: str
    snapshot: str
    category: str
    events: list[ScenarioAction]
    runtime_sha256: str | None = None
    assets_sha256: str | None = None
    stages: list[ScenarioStage] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def serialize_spec(self, handler):
        """Keep historical single-stage task bytes unchanged when unset."""
        data = handler(self)
        if not self.stages:
            data.pop("stages", None)
        return data


class ScenarioRecord(BaseModel):
    """One ordered, timestamped observation of the shared environment."""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    time: float
    started_time: float | None = None
    observed_time: float | None = None
    kind: Literal["tool", "environment", "user", "agent", "anchor", "error", "trigger"]
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: bool = False
    call_id: str | None = None
    event_id: str | None = None

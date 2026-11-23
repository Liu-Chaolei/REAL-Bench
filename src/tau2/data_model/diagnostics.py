"""Portable evidence contracts for independent, post-call diagnostics."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class EvidenceModel(BaseModel):
    """Persisted evidence rejects accidental undocumented fields."""

    model_config = ConfigDict(extra="forbid")


class UserInput(EvidenceModel):
    id: str
    text: str | None
    started: float
    ended: float
    stage: int | None = None
    event_id: str | None = None
    complete: bool = True
    role: Literal["user", "assistant_context"] = "user"
    unavailable_reason: str | None = None


class BackendInput(EvidenceModel):
    id: str
    content: Any
    time: float
    session_id: str | None = None
    task_id: str | None = None
    role: Literal["submit", "update", "prompt", "steer"] = "prompt"
    accepted: bool | None = None
    internal: bool = False
    logical_id: str | None = None


class OperationEvidence(EvidenceModel):
    call_id: str
    name: str
    app: str
    kind: Literal["READ", "WRITE", "SYSTEM", "UNKNOWN"]
    raw_arguments: dict[str, Any]
    arguments: dict[str, Any]
    result: Any = None
    error: bool = False
    started: float
    ended: float
    before_frame: str | None = None
    after_frame: str | None = None
    stage: int | None = None
    requestor: str = "assistant"


class DiagnosticEvidence(EvidenceModel):
    version: str = "tau-diagnostic-evidence/1"
    simulation_id: str
    task_id: str
    domain: str
    user_inputs: list[UserInput] = Field(default_factory=list)
    backend_inputs: list[BackendInput] = Field(default_factory=list)
    backend_status: Literal["not_applicable", "complete", "unavailable"] = (
        "not_applicable"
    )
    backend_reason: str | None = None
    operations: list[OperationEvidence] = Field(default_factory=list)
    frames: dict[str, dict] = Field(default_factory=dict)
    stages: dict = Field(default_factory=dict)
    audio: dict = Field(default_factory=dict)
    source: dict = Field(default_factory=dict)
    state_events: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    complete: bool = True


class DiagnosticEvidenceSource(Protocol):
    """Optional Agent hook; content is observed, never generated for scoring."""

    def get_diagnostic_evidence(self) -> dict: ...


class DomainExecutionAdapter(Protocol):
    """Domain-owned reference and effect rules independent of the Agent."""

    def evaluate(self, task: Any, evidence: DiagnosticEvidence, judge: Any) -> dict: ...


class DiagnosticEvaluator(Protocol):
    """A reproducible evaluator over a sealed evidence package."""

    def evaluate(self, task: Any, evidence: DiagnosticEvidence) -> dict: ...

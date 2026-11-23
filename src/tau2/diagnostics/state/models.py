"""State versions, semantic meanings and evidence-local opportunities."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal[
    "consistent", "inconsistent", "unsupported", "uncertain", "not_applicable"
]


class StateModel(BaseModel):
    """Small structured contracts shared by projection and interpretation."""

    model_config = ConfigDict(extra="forbid")


class Completion(StateModel):
    """An outcome condition, not an oracle trajectory or tool-call count."""

    mode: Literal["tool_result", "state_effect", "communication", "interaction"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    path: str = ""
    expected: Any = None
    comparison: Literal["exact", "contains", "semantic", "success"] = "exact"


class StateItem(StateModel):
    id: str
    kind: Literal[
        "goal", "business", "interaction", "fact", "constraint", "action", "claim"
    ]
    key: str
    value: Any = None
    status: str = "open"
    goal_id: str | None = None
    candidates: list[Any] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    visible_to: list[str] = Field(default_factory=lambda: ["agent"])
    visible_at: int = 0
    updated_at: int = 0
    completion: Completion | None = None
    history: list[dict] = Field(default_factory=list)
    blocked_by: str | None = None
    status_history: list[dict] = Field(default_factory=list)


class DialogueState(StateModel):
    version: int = 0
    goals: dict[str, StateItem] = Field(default_factory=dict)
    agenda: dict[str, StateItem] = Field(default_factory=dict)
    facts: dict[str, StateItem] = Field(default_factory=dict)
    constraints: dict[str, StateItem] = Field(default_factory=dict)
    operations: dict[str, StateItem] = Field(default_factory=dict)
    claims: dict[str, StateItem] = Field(default_factory=dict)
    uncertainty: list[dict] = Field(default_factory=list)
    dialogue: list[dict] = Field(default_factory=list)

    def visible(self, viewer):
        """Expose only information delivered to the behavior's own component."""
        value = self.model_dump(mode="json")
        for name in ("goals", "agenda", "facts", "constraints", "operations", "claims"):
            value[name] = {
                k: v
                for k, v in value[name].items()
                if viewer in v["visible_to"] or "all" in v["visible_to"]
            }
        for name in ("goals", "agenda", "facts", "constraints", "operations", "claims"):
            for item in value[name].values():
                history = [
                    h
                    for h in item["status_history"]
                    if viewer in h["visible_to"] or "all" in h["visible_to"]
                ]
                if item["status_history"]:
                    if history:
                        latest = history[-1]
                        item.update(
                            status=latest["status"],
                            source_refs=latest["source_refs"],
                            blocked_by=latest.get("blocked_by"),
                        )
                    else:
                        item["status"] = "unknown"
                        item["source_refs"] = []
                item["status_history"] = history
        for operation in value["operations"].values():
            operation["value"] = {
                k: v
                for k, v in (operation.get("value") or {}).items()
                if k not in ("observed_result", "observed_error")
            }
        value["dialogue"] = [
            r
            for r in value["dialogue"]
            if viewer in r["visible_to"] or "all" in r["visible_to"]
        ]
        value["uncertainty"] = [
            r for r in value["uncertainty"] if viewer in r.get("visible_to", [])
        ]
        return value


class DialogueEvent(StateModel):
    event_id: str
    kind: Literal[
        "user_turn",
        "agent_turn",
        "tool_requested",
        "tool_completed",
        "tool_delivered",
        "backend_observation",
    ]
    role: str
    sequence: int = 0
    timestamp: float | None = None
    content: Any = None
    source: str = "recorded"
    visible_to: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class ItemUpdate(StateModel):
    """Meaning inferred from a delivered user request, never hidden task text."""

    id: str
    kind: Literal["goal", "business", "interaction", "fact", "constraint"]
    key: str
    value: Any = None
    mode: Literal["assert", "correct", "deny", "cancel", "candidates"] = "assert"
    goal_id: str | None = None
    candidates: list[Any] = Field(default_factory=list)
    completion: Completion | None = None


class StateUse(StateModel):
    """One behavior's use of one relevant item; repeated mentions count once."""

    item_id: str
    mode: Literal["reply", "question", "tool_action", "commitment"]
    actual: Any = None
    path: str = ""
    status: Status
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class Fulfillment(StateModel):
    item_id: str
    actual: Any = None
    met: bool | None = None
    reason: str = ""
    evidence_id: str | None = None
    evidence_path: str = ""


class Interpretation(StateModel):
    """Per-event semantics; the projector alone changes state and computes scores."""

    updates: list[ItemUpdate] = Field(default_factory=list)
    uses: list[StateUse] = Field(default_factory=list)
    fulfills: list[Fulfillment] = Field(default_factory=list)
    advances: list[str] = Field(default_factory=list)
    regresses: list[str] = Field(default_factory=list)
    related_items: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)
    greeting: bool = False
    uncertain: bool = False


class StateDelta(StateModel):
    advanced_items: list[str] = Field(default_factory=list)
    satisfied_items: list[str] = Field(default_factory=list)
    regressed_items: list[str] = Field(default_factory=list)
    external_blocks: list[str] = Field(default_factory=list)
    goal_progress: list[str] = Field(default_factory=list)
    interaction_progress: list[str] = Field(default_factory=list)


class StateTransition(StateModel):
    window_id: str
    events: list[str] = Field(default_factory=list)
    state_before_agent: int | None = None
    state_before_tool_action: dict[str, int] = Field(default_factory=dict)
    state_before_agent_reply: dict[str, int] = Field(default_factory=dict)
    state_after_window: int = 0
    delta: StateDelta = Field(default_factory=StateDelta)
    progressivity: Literal[
        "forward", "stagnated", "regressed", "externally_blocked"
    ] = "stagnated"
    scoreable: bool = True
    closed_by_reply: bool = False
    opportunities: list[dict] = Field(default_factory=list)
    issues: list[dict] = Field(default_factory=list)

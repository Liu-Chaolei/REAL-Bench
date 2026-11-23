"""Contracts for the Gaia2 dynamic hidden-intent benchmark."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GoalKind = Literal[
    "tool_argument", "tool_action", "constraint", "communication", "report"
]
GoalVisibility = Literal["caller_visible", "withheld", "backend_only"]
HiddenNodeState = Literal[
    "withheld",
    "asked",
    "released",
    "satisfied",
    "invalid_question",
    "premature_action",
    "blocked",
]


class HiddenGoalNode(BaseModel):
    """One atomic requirement in a hidden-intent goal graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: GoalKind
    visibility: GoalVisibility
    field_type: str | None = None
    description: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    tool: str | None = None
    argument_path: str | None = None
    gold_value: Any = None
    depends_on: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    release_policy: Literal["always", "after_valid_question"] = "always"
    required: bool = True


class HiddenGoalGraph(BaseModel):
    """Complete private goal graph used by the dynamic user simulator."""

    model_config = ConfigDict(extra="forbid")

    version: str = "gaia2-hidden-goal-graph-v1"
    source_task_id: str
    source_category: str
    target_stage: int = Field(default=0, ge=0, le=2)
    public_request: str | None = None
    private_request: str | None = None
    nodes: list[HiddenGoalNode]
    edges: list[list[str]] = Field(default_factory=list)
    protected_values: list[str] = Field(default_factory=list)
    backend_only_values: list[str] = Field(default_factory=list)
    max_goal_expansions: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="after")
    def validate_graph(self) -> "HiddenGoalGraph":
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("goal graph node IDs must be unique")
        known = set(ids)
        for node in self.nodes:
            if not set(node.depends_on) <= known:
                raise ValueError(f"unknown dependency in goal node {node.id}")
        for edge in self.edges:
            if len(edge) != 2 or not set(edge) <= known:
                raise ValueError("goal graph edges must contain two known node IDs")
        withheld = [node for node in self.nodes if node.visibility == "withheld"]
        if any(node.release_policy != "after_valid_question" for node in withheld):
            raise ValueError("withheld nodes must use after_valid_question release")
        return self


class HiddenRequirement(BaseModel):
    """A withheld caller-provided requirement and its release contract."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    field: str
    gold_value: Any
    question_hint: str
    release_policy: Literal["after_valid_question"] = "after_valid_question"


class HiddenIntentPlan(BaseModel):
    """Public metadata for one hidden task, with private values kept separate."""

    model_config = ConfigDict(extra="forbid")

    version: str = "gaia2-hidden-intent-v1"
    task_id: str
    source_task_id: str
    source_task_sha256: str
    source_category: str
    target_stage: int = Field(ge=0, le=2)
    hidden_requirements: list[HiddenRequirement] = Field(min_length=1, max_length=3)
    visible_request: str = Field(min_length=1)
    private_request: str = Field(min_length=1)
    leakage_checks: dict[str, bool] = Field(default_factory=dict)


class HiddenGoalState(BaseModel):
    """Replayable state for one runtime graph revision."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=0, ge=0)
    node_states: dict[str, HiddenNodeState] = Field(default_factory=dict)
    released_values: dict[str, Any] = Field(default_factory=dict)
    asked_nodes: list[str] = Field(default_factory=list)
    transitions: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def from_graph(cls, graph: HiddenGoalGraph) -> "HiddenGoalState":
        return cls(
            node_states={
                node.id: "withheld" if node.visibility == "withheld" else "released"
                for node in graph.nodes
            }
        )

    def release(self, node: HiddenGoalNode, *, question: str) -> None:
        """Release one hidden value after a validated question."""
        if node.visibility != "withheld":
            raise ValueError(f"Node {node.id} is not withheld")
        state = self.node_states.get(node.id)
        if state == "released":
            self.transitions.append({"type": "repeat_question", "node_id": node.id})
            return
        if state not in {"withheld", "asked", "invalid_question", "premature_action"}:
            raise ValueError(f"Node {node.id} cannot be released from state {state}")
        self.node_states[node.id] = "released"
        self.released_values[node.id] = node.gold_value
        if node.id not in self.asked_nodes:
            self.asked_nodes.append(node.id)
        self.revision += 1
        self.transitions.append(
            {
                "type": "release",
                "node_id": node.id,
                "question": question,
                "revision": self.revision,
            }
        )

    def satisfy(self, node_id: str, *, tool: str, arguments: dict[str, Any]) -> bool:
        """Mark a released node satisfied when its bound tool argument is used."""
        node_state = self.node_states.get(node_id)
        if node_state != "released":
            return False
        self.node_states[node_id] = "satisfied"
        self.transitions.append(
            {
                "type": "satisfied",
                "node_id": node_id,
                "tool": tool,
                "arguments": arguments,
                "revision": self.revision,
            }
        )
        return True

    def mark_invalid_question(self, node_id: str | None, question: str) -> None:
        """Record a question that did not identify a releasable hidden node."""
        if node_id is not None and self.node_states.get(node_id) == "withheld":
            self.node_states[node_id] = "invalid_question"
        self.transitions.append(
            {"type": "invalid_question", "node_id": node_id, "question": question}
        )

    def mark_premature_action(self, node_id: str, action: str) -> None:
        """Record an action attempted before its hidden argument was released."""
        if self.node_states.get(node_id) == "withheld":
            self.node_states[node_id] = "premature_action"
        self.transitions.append(
            {"type": "premature_action", "node_id": node_id, "action": action}
        )

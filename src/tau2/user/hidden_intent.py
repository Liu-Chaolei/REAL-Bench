"""Runtime hidden-intent state and question-release helpers."""

from __future__ import annotations

import re
from typing import Any

from tau2.data_model.hidden_intent import HiddenGoalGraph, HiddenGoalState


def question_matches_node(question: str, *, description: str, field: str) -> bool:
    """Conservative lexical gate for a user release candidate.

    Production runs may replace this with a configured structured judge. The
    lexical gate is intentionally strict enough for offline tests and never
    releases a value merely because the Agent asked an unrelated question.
    """
    haystack = " ".join(question.casefold().split())
    terms = [term.casefold() for term in (description, field) if term]
    return any(term in haystack for term in terms)


class HiddenIntentRuntime:
    """Private graph state owned by the user simulator."""

    def __init__(self, graph: HiddenGoalGraph):
        self.graph = graph
        self.state = HiddenGoalState.from_graph(graph)

    def ask(self, question: str, *, node_id: str | None = None) -> dict[str, Any]:
        """Classify and, when valid, release one hidden node."""
        node = next((item for item in self.graph.nodes if item.id == node_id), None)
        if node is None or node.visibility != "withheld":
            self.state.mark_invalid_question(node_id, question)
            return {"released": False, "node_id": node_id, "reason": "unknown_node"}
        if not question_matches_node(
            question, description=node.description, field=node.argument_path or ""
        ):
            self.state.mark_invalid_question(node.id, question)
            return {"released": False, "node_id": node.id, "reason": "invalid_question"}
        self.state.release(node, question=question)
        return {
            "released": True,
            "node_id": node.id,
            "value": node.gold_value,
            "revision": self.state.revision,
        }

    def ask_auto(self, question: str) -> dict[str, Any]:
        """Release the first withheld node whose description matches the question."""
        if not re.search(
            r"\?|\b(?:can|could|would|what|which|where|when|who|how|tell|provide|give|share)\b",
            question,
            flags=re.IGNORECASE,
        ):
            return {"released": False, "node_id": None, "reason": "not_a_question"}
        for node in self.graph.nodes:
            if node.visibility != "withheld":
                continue
            if node.id not in self.state.released_values and question_matches_node(
                question, description=node.description, field=node.argument_path or ""
            ):
                return self.ask(question, node_id=node.id)
        self.state.mark_invalid_question(None, question)
        return {"released": False, "node_id": None, "reason": "no_matching_hidden_node"}

    def visible_values(self) -> dict[str, Any]:
        """Return only values already released to the caller-facing dialogue."""
        return dict(self.state.released_values)

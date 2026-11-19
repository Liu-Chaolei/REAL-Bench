"""Offline diagnostics for dynamic hidden-intent runs."""

from __future__ import annotations

from typing import Any

from tau2.data_model.hidden_intent import HiddenGoalGraph, HiddenGoalState


def score_hidden_intent(
    graph: HiddenGoalGraph,
    state: HiddenGoalState,
    *,
    task_passed: bool | None = None,
) -> dict[str, Any]:
    """Compute replayable hidden-information acquisition diagnostics.

    The final business reward remains owned by Gaia2. This report only measures
    whether the caller information protocol was followed.
    """
    hidden = [node for node in graph.nodes if node.visibility == "withheld"]
    released = [node for node in hidden if node.id in state.released_values]
    asked = [node for node in hidden if node.id in state.asked_nodes]
    premature = [
        event for event in state.transitions if event.get("type") == "premature_action"
    ]
    invalid = [
        event for event in state.transitions if event.get("type") == "invalid_question"
    ]
    denominator = len(hidden)
    return {
        "version": "gaia2-hidden-intent-metrics-v1",
        "hidden_count": denominator,
        "hidden_ask_rate": len(asked) / denominator if denominator else None,
        "hidden_reveal_rate": len(released) / denominator if denominator else None,
        "ask_specificity": 1.0 - (len(invalid) / max(len(state.transitions), 1)),
        "premature_action_rate": len(premature) / max(denominator, 1),
        "goal_graph_completion_rate": sum(
            state.node_states.get(node.id) == "satisfied" for node in graph.nodes if node.required
        )
        / max(sum(node.required for node in graph.nodes), 1),
        "task_pass_at_1": task_passed,
        "state_revision": state.revision,
        "transition_count": len(state.transitions),
    }

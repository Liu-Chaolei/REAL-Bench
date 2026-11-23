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
    asked = [node for node in hidden if node.id in state.asked_nodes]
    denominator = len(hidden)
    return {
        "version": "gaia2-hidden-intent-metrics-v1",
        "hidden_count": denominator,
        "hidden_ask_rate": len(asked) / denominator if denominator else None,
    }

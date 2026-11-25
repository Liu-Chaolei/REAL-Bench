"""Post-run scoring for authored Gaia2 interaction variants."""

from tau2.metrics.active.persistent_task.evaluator import score_persistent_task


def score_interaction_variant(summary, scoring=None):
    """Score saved variant facts without constructing a controller."""
    kind = summary.get("variant_type")
    status = summary.get("status")
    binding_status = summary.get("binding_status") or {}
    if kind == "addtask" or status in {
        "target_unbound",
        "target_finished_before_request",
        "queued",
    }:
        return {
            "status": "not_applicable" if kind == "addtask" else status,
            "completion_score": None,
            "evaluation_log": {
                "native_result": {"passed": None},
                "reply": {"passed": None, "status": "not_evaluated"},
            },
        }
    if (
        kind == "cancel"
        and "temporary" in binding_status
        and binding_status.get("temporary") != "bound"
    ):
        return {
            "status": "target_unbound",
            "completion_score": None,
            "evaluation_log": {
                "native_result": {
                    "passed": None,
                    "reason": binding_status.get("temporary", "unbound"),
                },
                "reply": {"passed": None, "status": "not_evaluated"},
            },
        }
    if (
        kind == "modify"
        and "primary" in binding_status
        and binding_status.get("primary") != "bound"
    ):
        return {
            "status": "target_unbound",
            "completion_score": None,
            "evaluation_log": {
                "native_result": {
                    "passed": None,
                    "reason": binding_status.get("primary", "unbound"),
                },
                "reply": {"passed": None, "status": "not_evaluated"},
            },
        }
    return score_persistent_task(summary, scoring)


class InteractionVariantEvaluator:
    """Evaluate saved facts without driving a scenario."""

    score = staticmethod(score_interaction_variant)

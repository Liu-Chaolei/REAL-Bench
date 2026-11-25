"""Post-run scoring of serializable persistent-task facts, without runtime access."""

import json
from typing import Any

from tau2.persistent_tasks.scores import consolidate_score


def score_persistent_task(summary, scoring=None) -> dict[str, Any]:
    """Score the resolved request independently of the task reward."""
    status = summary.get("status")
    binding = summary.get("binding") or {}
    operations = summary.get("operations") or []
    delivery = summary.get("delivery") or {}
    reply_text = summary.get("reply_text") or ""
    if status in {
        "unsupported",
        "generation_unavailable",
        "unresolved",
        "not_reached",
        "reply_unavailable",
    }:
        return consolidate_score(
            {
                "status": status,
                "completion_score": None,
                "evaluation_log": {
                    "operation_reward": None,
                    "native_result": {"passed": None, "reason": status},
                    "reply": {
                        "passed": None,
                        "status": "not_evaluated",
                        "reason": status,
                    },
                },
            }
        )
    resolved = (
        summary.get("resolved_intent")
        or (summary.get("generated_request") or {}).get("resolved_intent")
        or {}
    )
    operation_name = resolved.get("operation")
    candidates = [
        event
        for event in operations
        if event.get("backend_task_id") == binding.get("backend_task_id")
        and event.get("kind") != "native_operation_started"
        and delivery.get("delivered_at") is not None
        and event.get("timestamp", 0) >= delivery.get("delivered_at")
        and event.get("success") is not False
        and event.get("result") is not None
        and next(
            (
                start.get("timestamp", 0)
                for start in operations
                if event.get("call_id")
                and start.get("call_id") == event["call_id"]
                and start.get("kind") == "native_operation_started"
            ),
            event.get("timestamp", 0),
        )
        >= delivery.get("delivered_at")
    ]
    operation = next(
        (
            event
            for event in candidates
            if event.get("operation") == operation_name
            or (operation_name == "result" and event.get("operation") == "query")
        ),
        None,
    )
    operation_reward = float(operation is not None)
    result = operation.get("result") if operation else None
    actual_state = (
        (result.get("state") if isinstance(result, dict) else None)
        or (operation.get("state") if operation else None)
        or (operation.get("status") if operation else None)
    )
    native_reward = float(
        operation_reward
        and (
            (operation_name == "query" and actual_state is not None)
            or (
                operation_name == "result"
                and actual_state == "completed"
                and isinstance(result, dict)
                and result.get("result_available", True)
            )
            or (
                operation_name == "modify"
                and isinstance(result, dict)
                and result.get("accepted") is True
            )
            or (
                operation_name == "cancel"
                and isinstance(result, dict)
                and result.get("effect") == "cancelled"
            )
        )
    )
    reply_reward = 0.0
    reply_detail = {
        "passed": None,
        "status": "not_evaluated",
        "reason": "native_result_not_passed",
    }
    if native_reward and not reply_text:
        reply_detail = {
            "passed": False,
            "status": "no_reply",
            "reason": "no_delivered_reply",
        }
    if reply_text and native_reward:
        fact = json.dumps(
            {
                "operation": operation_name,
                "actual_state": actual_state,
                "native_operation_result": result,
                "backend_task_id": binding.get("backend_task_id"),
                "task_objective": binding.get("task_label"),
                "requirement": "Accurately report the actual native task result without inventing completion or errors.",
            },
            ensure_ascii=False,
        )
        if scoring is None:
            reply_reward = 1.0
            reply_detail = {
                "passed": True,
                "status": "presence_only",
                "reason": "scoring_not_configured",
            }
        else:
            from tau2.data_model.message import AssistantMessage
            from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

            verdict = CommunicateEvaluator.evaluate_communicate_info(
                [AssistantMessage(role="assistant", content=reply_text)],
                [fact],
                scoring=scoring,
            )[0]
            reply_reward = float(verdict.met)
            reply_detail = {
                "passed": verdict.met,
                "status": "judged",
                "reason": verdict.justification,
            }
    reward = native_reward * reply_reward
    return {
        "status": "measured",
        "completion_score": reward,
        "evaluation_log": {
            "operation_reward": operation_reward,
            "native_result": {
                "passed": bool(native_reward),
                "operation": operation,
                "actual_state": actual_state,
            },
            "reply": {**reply_detail, "text": reply_text},
        },
        "operation": operation,
        "actual_state": actual_state,
    }

"""Lightweight checks for persistent task operations."""

from __future__ import annotations

from typing import Any

from tau2.persistent_tasks.backend import BackendTaskObservation
from tau2.persistent_tasks.models import PersistentTask, TaskTraceRecord
from tau2.persistent_tasks.scores import consolidate_score


def evaluate_operations(
    expected: list[dict[str, Any]], actual: list[TaskTraceRecord]
) -> dict[str, Any]:
    """Evaluate expected request operations against normalized trace records."""
    records = [
        record
        if isinstance(record, TaskTraceRecord)
        else TaskTraceRecord.model_validate(record)
        for record in actual
    ]
    grouped = {}
    for record in records:
        if record.request_id:
            grouped.setdefault(record.request_id, []).append(record)
    expected_ids = {item["request_id"] for item in expected}
    duplicate_ids = sorted(
        request_id for request_id, rows in grouped.items() if len(rows) > 1
    )
    unexpected_ids = sorted(set(grouped) - expected_ids)
    checks = []
    for item in expected:
        rows = grouped.get(item["request_id"], [])
        record = rows[0] if len(rows) == 1 else None
        correct = (
            record is not None
            and record.operation == item["operation"]
            and record.source_task_id == item.get("source_task_id")
            and record.source_stage == item.get("source_stage")
        )
        if correct and item.get("expected_state"):
            correct = (
                record.state_after == item["expected_state"]
                or record.state_before == item["expected_state"]
            )
        if correct and item.get("expected_milestone"):
            result = record.result
            completed = (
                result.get("completed_milestones", [])
                if isinstance(result, dict)
                else getattr(result, "completed_milestones", set())
            )
            correct = item["expected_milestone"] in completed
        checks.append(
            {
                "request_id": item["request_id"],
                "met": correct,
                "duplicate": len(rows) > 1,
                "actual": record.model_dump(mode="json") if record else None,
            }
        )
    valid = all(check["met"] for check in checks)
    valid = valid and not duplicate_ids and not unexpected_ids
    return {
        "reward": float(valid),
        "checks": checks,
        "duplicate_request_ids": duplicate_ids,
        "unexpected_request_ids": unexpected_ids,
    }


def evaluate_final_state(
    expected: dict[str, dict[str, Any]], tasks: list[PersistentTask]
) -> dict[str, Any]:
    """Check only explicitly expected final task states."""
    checks = []
    actual = {(task.source_task_id, task.source_stage): task for task in tasks}
    for target, fields in expected.items():
        task = actual.get(tuple(target))
        met = task is not None and all(
            getattr(task, key) == value for key, value in fields.items()
        )
        checks.append({"target": target, "met": met})
    return {"reward": float(all(check["met"] for check in checks)), "checks": checks}


def evaluate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one serialized sidecar trace without reconstructing an env."""
    operations = summary.get("trace", {}).get("operations", [])
    operation_result = evaluate_operations(
        summary.get("expected_operations", []), operations
    )
    replies = summary.get("replies", {})
    reply_checks = []
    for check in summary.get("expected_checks", []):
        request_id = check.get("request_id")
        reply = replies.get(request_id, "")
        met = bool(reply) if check.get("check_reply") else True
        reply_checks.append({"request_id": request_id, "met": met, "reply": reply})
    reply_reward = float(all(row["met"] for row in reply_checks))
    return consolidate_score(
        {
            "reward": operation_result["reward"] * reply_reward,
            "evaluation_log": {"operation_reward": operation_result["reward"]},
            "reply_reward": reply_reward,
            "checks": operation_result["checks"],
            "reply_checks": reply_checks,
        }
    )


def evaluate_backend_operations(
    expected: list[dict[str, Any]], actual: list[BackendTaskObservation]
) -> dict[str, Any]:
    """Strictly match canonical operations captured from a native backend."""
    grouped: dict[str, list[BackendTaskObservation]] = {}
    for observation in actual:
        if observation.request_id:
            grouped.setdefault(observation.request_id, []).append(observation)
    expected_ids = {item["request_id"] for item in expected}
    duplicate_ids = sorted(key for key, rows in grouped.items() if len(rows) > 1)
    unexpected_ids = sorted(set(grouped) - expected_ids)
    checks = []
    for item in expected:
        rows = grouped.get(item["request_id"], [])
        observation = rows[0] if len(rows) == 1 else None
        met = (
            observation is not None
            and observation.operation == item.get("operation")
            and observation.source_task_id == item.get("source_task_id")
            and observation.source_stage == item.get("source_stage")
        )
        checks.append(
            {
                "request_id": item["request_id"],
                "met": met,
                "duplicate": len(rows) > 1,
                "actual": observation.__dict__ if observation else None,
            }
        )
    return {
        "reward": float(
            all(check["met"] for check in checks)
            and not duplicate_ids
            and not unexpected_ids
        ),
        "checks": checks,
        "duplicate_request_ids": duplicate_ids,
        "unexpected_request_ids": unexpected_ids,
    }

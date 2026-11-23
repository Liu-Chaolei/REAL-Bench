"""Frozen native evidence checks and evidence-grounded semantic reply grading."""

import hashlib
import json

from tau2.data_model.message import AssistantMessage
from tau2.data_model.semantic_communication import CommunicateScorer
from tau2.evaluator.evaluator_communicate import CommunicateEvaluator
from tau2.evaluator.persistent_tasks.coverage import COVERAGE_FIELDS
from tau2.evaluator.persistent_tasks.operation import (
    request_operations,
    timing_is_valid,
)
from tau2.persistent_tasks.core.models import CanonicalState
from tau2.persistent_tasks.legacy.native_reply import reply_text
from tau2.persistent_tasks.legacy.native_result import result_state
from tau2.persistent_tasks.runtime.replay import replay
from tau2.persistent_tasks.scores import consolidate_score

GRADING_VERSION = "qwen-persistent-grading-v7"


def grade(summary, scoring, *, reply_judge=None):
    """Verify replay before grading; never count an unasked request as success."""
    replay(summary)
    if scoring.scorer != CommunicateScorer.SEMANTIC:
        raise ValueError("persistent replies require semantic communication scoring")
    settings = scoring.model_dump(mode="json")
    if settings.get("judge"):
        settings["judge"]["model_args"].pop("api_key", None)
    cache_key = hashlib.sha256(
        json.dumps(
            [summary["evidence_sha256"], settings, GRADING_VERSION],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cached = summary.get("grading")
    if reply_judge is None and cached and cached.get("cache_key") == cache_key:
        return cached
    checks = []
    reply_checks = []
    coverage = {key: 0 for key in COVERAGE_FIELDS}
    for request in summary["config"]["requests"]:
        key = request["request_id"]
        target = next(
            (b for b in summary["config"]["bindings"] if b["role"] == request["role"]),
            None,
        )
        release = summary["releases"][key]
        coverage["expected"] += 1
        coverage["activated"] += int(
            any(
                item["kind"] == "activate_stage"
                and target
                and item["stage"] == target["source_stage"]
                for item in summary["journal"]
            )
        )
        for metric, field in (
            ("condition_met", "condition_met_at"),
            ("queued", "queued_at"),
            ("delivery_started", "delivery_started_at"),
            ("delivered", "delivered_at"),
        ):
            coverage[metric] += int(release[field] is not None)
        all_operations = request_operations(summary, key)
        helpers = [
            op
            for op in all_operations
            if request.get("allow_status_list_lookup")
            and op["operation"] == "query"
            and (op.get("arguments") or {}).get("list_all") is True
        ]
        operations = [op for op in all_operations if op not in helpers]
        valid_helpers = len(helpers) <= 1 and all(
            op.get("success") is True
            and op.get("completed_at") is not None
            and isinstance(op.get("result"), dict)
            and isinstance(op["result"].get("tasks"), list)
            for op in helpers
        )
        op = operations[0] if len(operations) == 1 else None
        identity = op.get("identity") if op else None
        operation = bool(
            op
            and valid_helpers
            and identity
            and target
            and release["delivered_at"] is not None
            and op["success"] is True
            and op["completed_at"] is not None
            and timing_is_valid(op, request, release)
            and identity["role"] == request["role"]
            and identity["source_task_id"] == target["source_task_id"]
            and identity["source_stage"] == target["source_stage"]
            and identity["backend_task_id"] == op["backend_task_id"]
            and (
                op["operation"] == request["operation"]
                or (request["operation"] == "result" and op["operation"] == "query")
            )
            and all(
                (op.get("arguments") or {}).get(k) == v
                for k, v in request["expected_arguments"].items()
            )
        )
        result = op.get("result") if op else None
        native_state = result_state(result)
        state_result = bool(operation and native_state.value == request["state"])
        if request["operation"] == "modify":
            instruction = (op.get("arguments") or {}).get("instruction") if op else None
            confirmed = any(
                item["kind"] == "observation"
                and item["observation"].get("backend_task_id")
                == (op or {}).get("backend_task_id")
                and item["observation"]["timestamp"]
                >= (op or {}).get("started_at", float("inf"))
                and any(
                    update.get("instruction") == instruction
                    and update.get("status") == "delivered"
                    for update in (item["observation"].get("raw") or {})
                    .get("task", {})
                    .get("updates", [])
                )
                for item in summary["journal"]
            )
            state_result = bool(
                operation
                and instruction
                and confirmed
                and isinstance(result, dict)
                and result.get("status") == "accepted"
            )
        elif request["operation"] == "cancel":
            state_result = bool(
                operation
                and isinstance(result, dict)
                and result.get("status") == "cancelled"
                and op.get("state") == "cancelled"
            )
        if request["operation"] == "result":
            state_result = (
                state_result
                and "result" in result
                and native_state == CanonicalState.COMPLETED
            )
        window = summary["reply_windows"].get(key, {})
        text = reply_text(summary, key)
        coverage["answered"] += int(bool(text))
        fact = json.dumps(
            {
                "user_request": request["text"],
                "requirement": request["reply_expectation"],
                "native_operation_result": result,
                "rule": "Accurately answer this request using this native result; contradictory or invented status/results fail.",
            },
            ensure_ascii=False,
        )
        verdict = {
            "met": False,
            "justification": "No delivered reply backed by a valid native operation",
        }
        if text and operation and state_result:
            prior = next(
                (
                    check
                    for check in (cached or {})
                    .get("evaluation_log", {})
                    .get("reply", {})
                    .get("checks", (cached or {}).get("reply_checks", []))
                    if check.get("request_id") == key
                ),
                None,
            )
            if reply_judge is not None:
                verdict = reply_judge(text, fact)
            elif (
                prior
                and prior.get("info") == fact
                and prior.get("window") == window
                and cached.get("judge") == settings
            ):
                verdict = {
                    field: prior[field] for field in ("info", "met", "justification")
                }
            else:
                verdict = CommunicateEvaluator.evaluate_communicate_info(
                    [AssistantMessage(role="assistant", content=text)],
                    [fact],
                    scoring=scoring,
                )[0].model_dump(mode="json")
        checks.append(
            {
                "request_id": key,
                "operation": operation,
                "state_result": state_result,
                "actual_operation_count": len(all_operations),
                "auxiliary_lookup_count": len(helpers),
                "release_status": release["status"],
            }
        )
        reply_checks.append(
            {
                "request_id": key,
                **verdict,
                "window": window,
                "evaluated": bool(text and operation and state_result),
            }
        )
    complete = not summary["issues"]
    not_reached = any(
        summary["releases"][request["request_id"]]["delivery_started_at"] is None
        for request in summary["config"]["requests"]
    )
    reply_unavailable = summary.get(
        "reply_evidence_version"
    ) == "persistent-reply-v2" and any(
        not reply_text(summary, request["request_id"])
        for request in summary["config"]["requests"]
    )
    operation_reward = float(all(c["operation"] for c in checks))
    state_reward = float(all(c["state_result"] for c in checks))
    reply_reward = float(all(c["met"] for c in reply_checks))
    return consolidate_score(
        {
            "version": GRADING_VERSION,
            "status": (
                "not_reached"
                if not_reached
                else (
                    "evidence_unavailable"
                    if not complete
                    else ("reply_unavailable" if reply_unavailable else "completed")
                )
            ),
            "reward": state_reward * reply_reward
            if complete and not not_reached and not reply_unavailable
            else None,
            "evaluation_log": {
                "operation_reward": operation_reward,
                "coverage": coverage,
                "reply": {
                    "passed": bool(reply_reward)
                    if all(c["evaluated"] for c in reply_checks)
                    else None,
                    "status": "judged"
                    if all(c["evaluated"] for c in reply_checks)
                    else "not_fully_evaluated",
                },
            },
            "native_result_reward": state_reward,
            "reply_reward": reply_reward,
            "checks": checks,
            "reply_checks": reply_checks,
            "judge": settings,
            "cache_key": hashlib.sha256(
                json.dumps(
                    [summary["evidence_sha256"], settings, GRADING_VERSION],
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        }
    )

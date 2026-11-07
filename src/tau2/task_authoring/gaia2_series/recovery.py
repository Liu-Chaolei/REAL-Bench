"""Resumable repair orchestration for a completed or interrupted full run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tau2.task_authoring.gaia2_series import pipeline
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    RequestPolicy,
    SeriesDraft,
    StageDraft,
)
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    read_record,
    source_tasks,
    write_json,
)
from tau2.task_authoring.gaia2_series.verification import verify_draft

GENERATION_STATUSES = {
    "needs_revision",
    "partial",
    "generating",
    "verification_error",
    "needs_reverification",
}


def recover_candidates(dataset, task_ids, max_attempts=3):
    """Recheck saved replies without model calls and retain prior attempt counts."""
    from tau2.utils.llm_utils import extract_json_from_llm_response

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    report = []
    for task_id in task_ids:
        record = read_record(dataset, task_id)
        if record.get("status") != "verification_error":
            report.append({"id": task_id, "status": "unchanged"})
            continue
        draft = (
            SeriesDraft.model_validate(record["draft"])
            if record.get("draft")
            else SeriesDraft(
                base_task_id=task_id, base_task_sha256=digest(tasks[task_id])
            )
        )
        number = len(draft.stages) + 1
        reply = next(
            (
                call
                for call in reversed(record.get("calls", []))
                if call.get("content")
                and call.get("purpose") in {"StageDraft", "RepairResult"}
            ),
            None,
        )
        if number > 2 or reply is None:
            raise ValueError(f"No saved candidate reply for {task_id}")
        payload = json.loads(extract_json_from_llm_response(reply["content"]))
        stage = StageDraft.model_validate(payload.get("stage", payload))
        candidate = draft.model_copy(deep=True)
        candidate.stages.append(stage)
        try:
            verification, execution = verify_draft(
                tasks[task_id], Path(manifest["source_root"]), candidate
            )
        except CandidateValidationError as error:
            key = str(number)
            bucket = (
                "repair_attempts"
                if record.get("attempts", {}).get(key, 0) >= max_attempts
                else "attempts"
            )
            record.setdefault(bucket, {})[key] = min(
                max_attempts, record.get(bucket, {}).get(key, 0) + 1
            )
            record.setdefault("candidate_history", []).append(
                {
                    "stage": number,
                    "valid": False,
                    "candidate": stage.model_dump(mode="json"),
                    "feedback": error.details,
                    "error": str(error),
                    "reclassified": True,
                }
            )
            record.update(
                status="needs_revision",
                last_error=str(error),
                validation_failure=error.details,
            )
        else:
            record.update(
                draft=candidate.model_dump(mode="json"),
                verification={
                    **verification,
                    "implementation_sha256": pipeline.implementation_digest(),
                },
                status="verified" if number == 2 else "partial",
                last_error="",
            )
            record.pop("review", None)
            record.pop("human", None)
            write_json(
                dataset / "journals" / f"{digest(task_id)}.json", execution.records
            )
        pipeline._persist(dataset, record)
        report.append({"id": task_id, "status": record["status"]})
    write_json(dataset / "candidate-reclassification.json", report)
    return report


def utc_now() -> str:
    """Return an explicit UTC timestamp for recovery checkpoints."""
    return datetime.now(timezone.utc).isoformat()


def recovery_scope(dataset: Path) -> dict[str, list[str]]:
    """Classify records without changing them or spending model calls."""
    _, tasks = source_tasks(dataset)
    generation: list[str] = []
    review: list[str] = []
    for task_id in sorted(tasks):
        record = read_record(dataset, task_id)
        status = record.get("status", "pending")
        stages = len((record.get("draft") or {}).get("stages", []))
        if pipeline.rejected_stage(record) is not None:
            generation.append(task_id)
        elif status in GENERATION_STATUSES:
            (review if stages == 2 else generation).append(task_id)
        elif status == "provider_error":
            (review if stages == 2 else generation).append(task_id)
        elif status == "review_error":
            review.append(task_id)
        elif status == "verified" and stages == 2:
            review.append(task_id)
    return {"generation": generation, "review": review}


def _complete_unreviewed(dataset: Path) -> list[str]:
    """Return complete drafts needing a fresh review after repair or retry."""
    _, tasks = source_tasks(dataset)
    ids = []
    for task_id in sorted(tasks):
        record = read_record(dataset, task_id)
        if (
            len((record.get("draft") or {}).get("stages", [])) == 2
            and record.get("status") != "reviewed"
            and pipeline.rejected_stage(record) is None
        ):
            ids.append(task_id)
    return ids


def _snapshot(dataset: Path, tasks: dict) -> dict[str, dict]:
    """Read task-level content progress without counting calls or replay events."""
    result = {}
    for task_id in tasks:
        record = read_record(dataset, task_id)
        result[task_id] = {
            key: record[key]
            for key in ("status", "draft", "recovery_stop")
            if key in record
        }
    return result


def _content_state(records: dict[str, dict]) -> dict[str, str]:
    return {
        task_id: digest(
            {
                "draft": record.get("draft"),
                "reviewed": record.get("status") == "reviewed",
            }
        )
        for task_id, record in records.items()
    }


def _progress(before: dict[str, dict], after: dict[str, dict]) -> dict:
    def stages(record):
        return len((record.get("draft") or {}).get("stages", []))

    accepted_before = {k for k, r in before.items() if r.get("status") == "reviewed"}
    accepted_after = {k for k, r in after.items() if r.get("status") == "reviewed"}
    return {
        "reviewed_before": len(accepted_before),
        "reviewed_after": len(accepted_after),
        "new_reviewed": len(accepted_after - accepted_before),
        "new_completed_stages": sum(
            max(0, stages(r) - stages(before[k])) for k, r in after.items()
        ),
    }


def run_recovery(
    dataset: Path,
    config: AuthoringConfig,
    *,
    concurrency: int,
    repair_rounds: int,
    request_policy: RequestPolicy,
) -> dict:
    """Repair all recoverable records, then verify and independently review them."""
    if repair_rounds < 1:
        raise ValueError("repair_rounds must be positive")
    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    pipeline._check_open(manifest)
    state_path = dataset / "recovery-run.json"
    before = _snapshot(dataset, tasks)
    state = {
        "status": "running",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "repair_rounds": repair_rounds,
        "request_policy": request_policy.model_dump(mode="json"),
        "scope": recovery_scope(dataset),
        "release": False,
    }
    write_json(state_path, state)

    def checkpoint(phase: str, report: dict | None = None) -> None:
        state["phase"] = phase
        if report is not None:
            state.setdefault("reports", []).append(report)
        state["updated_at"] = utc_now()
        write_json(state_path, state)

    try:
        scope = state["scope"]
        if scope["generation"]:
            report = pipeline.generate_dataset(
                dataset,
                config,
                scope["generation"],
                concurrency=concurrency,
                request_policy=request_policy,
                repair=True,
                recovery_rounds=repair_rounds,
            )
            checkpoint("generation", report)

        for cycle in range(3):
            cycle_before = _content_state(_snapshot(dataset, tasks))
            verify_ids = _complete_unreviewed(dataset)
            if verify_ids:
                checkpoint("verification")
                verification = pipeline.verify_dataset(dataset, verify_ids, concurrency)
                checkpoint("verification", verification)
            review_ids = _complete_unreviewed(dataset)
            if not review_ids:
                break
            report = pipeline.review_dataset(
                dataset,
                config,
                review_ids,
                concurrency=concurrency,
                request_policy=request_policy,
            )
            checkpoint(f"review-{cycle + 1}", report)
            rejected = [
                item["id"]
                for item in report.get("results", [])
                if item.get("status") in {"needs_revision", "rejected"}
            ]
            if not rejected:
                break
            report = pipeline.generate_dataset(
                dataset,
                config,
                rejected,
                concurrency=concurrency,
                request_policy=request_policy,
                repair=True,
                recovery_rounds=repair_rounds,
            )
            checkpoint(f"review-repair-{cycle + 1}", report)
            if all(item.get("skip_reason") for item in report.get("results", [])):
                state["stop_reason"] = "repair_tasks_stalled"
                break
            if _content_state(_snapshot(dataset, tasks)) == cycle_before:
                state["stop_reason"] = "no_candidate_progress"
                break

        after = _snapshot(dataset, tasks)
        scope_after = recovery_scope(dataset)
        state["remaining_scope"] = scope_after
        state["progress"] = _progress(before, after)
        state["made_progress"] = bool(
            state["progress"]["new_reviewed"]
            or state["progress"]["new_completed_stages"]
        )
        state["blocked_tasks"] = {
            task_id: record["recovery_stop"]
            for task_id, record in after.items()
            if record.get("recovery_stop")
        }
        state["unresolved_tasks"] = [
            task_id
            for task_id, record in after.items()
            if record.get("status") != "reviewed"
            or len((record.get("draft") or {}).get("stages", [])) != 2
        ]
        state["status"] = "needs_attention" if state["unresolved_tasks"] else "complete"
        if state["status"] != "complete" and not state["made_progress"]:
            state["stop_reason"] = "no_progress"
        state["updated_at"] = utc_now()
        write_json(state_path, state)
        return state
    except BaseException as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}")
        state["updated_at"] = utc_now()
        write_json(state_path, state)
        raise

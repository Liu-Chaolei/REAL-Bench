"""Local, model-free takeover workflow for failed Gaia2 series tasks."""

from __future__ import annotations

import copy
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tau2.task_authoring.gaia2_series import pipeline
from tau2.task_authoring.gaia2_series.schema import SeriesDraft, StageDraft
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    read_record,
    record_path,
    source_tasks,
    write_json,
)
from tau2.task_authoring.gaia2_series.verification import verify_draft

SESSION = "manual-takeover-v1"


def preflight_submission(dataset: Path, submission: dict) -> dict:
    """Validate a candidate without executing tools or changing work records."""
    from tau2.task_authoring.gaia2_series.questions import QuestionPlan

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    tid = submission["task_id"]
    scope = json.loads((dataset / "manual-scope.json").read_text())
    if tid not in scope["failed_ids"]:
        raise ValueError("Task is outside the manual repair scope")
    record = read_record(dataset, tid)
    if record.get("manual_status") == "verified":
        return {"task_id": tid, "status": "already_verified"}
    base_value = record.get("manual_pending_candidate", record.get("draft"))
    if submission.get("base_candidate_sha256") != digest(base_value):
        raise ValueError("Stale base candidate hash")
    plan = QuestionPlan.model_validate(submission.get("question_plan", record["question_plan"]))
    draft = SeriesDraft.model_validate(submission["candidate"])
    if draft.base_task_id != tid or draft.base_task_sha256 != digest(tasks[tid]):
        raise ValueError("Candidate base task identity differs from sealed source")
    requests = [q.request for q in plan.requests]
    if len(draft.stages) != 2 or [s.request for s in draft.stages] != requests:
        raise ValueError("Two candidate requests must match the submitted question plan")
    event_ids = {e.id for stage in draft.stages for e in stage.events}
    if len(event_ids) != sum(len(stage.events) for stage in draft.stages):
        raise ValueError("Duplicate event IDs")
    for stage in draft.stages:
        for evidence in stage.evidence:
            if evidence.expected is None:
                raise ValueError("Evidence expected values cannot be null")
            if evidence.public_tool.startswith("AgentUserInterface__"):
                raise ValueError("AgentUserInterface cannot provide factual evidence")
    # compile_series performs identity, dependency, template and evidence timing checks
    from tau2.task_authoring.gaia2_series.compiler import compile_series

    compile_series(tasks[tid], draft, allow_current_evidence=False)
    return {"task_id": tid, "status": "preflight_ok", "candidate_sha256": digest(draft)}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_route(record: dict) -> str:
    """Classify a failed record without making a model call."""
    stages = len((record.get("draft") or {}).get("stages", []))
    status = record.get("status")
    if status in {"provider_error", "verification_error", "review_error"}:
        return "system_error"
    if stages == 2:
        return "full_candidate"
    if stages == 1:
        return "stage2_missing"
    return "stage1_missing"


def _failed_ids(source_dataset: Path) -> tuple[list[str], list[str]]:
    scope = json.loads((source_dataset / "scope.json").read_text())
    ids = set(scope["repair_order"])
    records = [read_record(source_dataset, task_id) for task_id in sorted(ids)]
    passed = [
        record["base_task_id"]
        for record in records
        if record.get("status") == "reviewed"
    ]
    failed = [
        record["base_task_id"]
        for record in records
        if record.get("status") != "reviewed"
    ]
    if len(passed) != 68 or len(failed) != 314:
        raise ValueError(
            f"Expected 68 passed and 314 failed records, got {len(passed)} and {len(failed)}"
        )
    return passed, failed


def initialize_manual_dataset(source_dataset: Path, output_dataset: Path) -> dict:
    """Create an isolated failed-task overlay while preserving successful records."""
    source_dataset = Path(source_dataset).resolve()
    output_dataset = Path(output_dataset).resolve()
    if output_dataset.exists():
        raise ValueError(f"Manual dataset already exists: {output_dataset}")
    # Initialization deliberately does not import the current runtime.  The
    # source dataset is bound to the isolated runtime that created it; callers
    # run verification under that same snapshot later.
    manifest = json.loads((source_dataset / "manifest.json").read_text())
    if manifest.get("expected_tasks") != 800:
        raise ValueError("Manual takeover requires the sealed 800-task population")
    passed, failed = _failed_ids(source_dataset)
    staging = output_dataset.with_name(output_dataset.name + ".preparing")
    if staging.exists():
        raise ValueError(f"Incomplete manual preparation exists: {staging}")
    staging.mkdir(parents=True)
    for name in ("manifest.json", "generation-config.json"):
        shutil.copy2(source_dataset / name, staging / name)
    shutil.copytree(source_dataset / "indexes", staging / "indexes")
    (staging / "work").mkdir()
    (staging / "journals").mkdir()
    entries = []
    for task_id in failed:
        source_path = record_path(source_dataset, task_id)
        record = json.loads(source_path.read_text())
        record["manual_session"] = SESSION
        record["manual_route"] = classify_route(record)
        record["manual_attempt"] = 0
        record["manual_candidate_sha256"] = (
            digest(record.get("draft")) if record.get("draft") else None
        )
        record["manual_base_candidate_sha256"] = record["manual_candidate_sha256"]
        record["manual_last_error"] = record.get("last_error", "")
        record["manual_status"] = "pending"
        record["manual_history"] = []
        write_json(record_path(staging, task_id), record)
        journal = source_dataset / "journals" / f"{digest(task_id)}.json"
        if journal.exists():
            shutil.copy2(journal, staging / "journals" / journal.name)
        entries.append(
            {
                "task_id": task_id,
                "route": record["manual_route"],
                "source_record_sha256": file_digest(source_path),
                "candidate_sha256": record["manual_candidate_sha256"],
                "status": "pending",
            }
        )
    child_manifest = copy.deepcopy(manifest)
    child_manifest.update(
        parent_dataset=str(source_dataset),
        run_kind="manual-takeover-v1",
        manual_session=SESSION,
        manual_failed_ids=failed,
        manual_preserved_ids=passed,
        expected_tasks=800,
        status="authoring",
        execution_ready=False,
    )
    write_json(staging / "manifest.json", child_manifest)
    write_json(
        staging / "manual-scope.json",
        {
            "session": SESSION,
            "created_at": utc_now(),
            "source_dataset": str(source_dataset),
            "expected_tasks": 800,
            "preserved_passed_count": len(passed),
            "failed_count": len(failed),
            "preserved_passed_ids": passed,
            "failed_ids": failed,
            "entries": entries,
        },
    )
    write_json(
        staging / "manual-state.json",
        {
            "session": SESSION,
            "phase": "initialized",
            "updated_at": utc_now(),
            "status_counts": dict(Counter(entry["status"] for entry in entries)),
            "route_counts": dict(Counter(entry["route"] for entry in entries)),
            "manual_verified": 0,
            "manual_blocked": 0,
        },
    )
    if not (staging / "manifest.json").exists():
        raise ValueError("Manual manifest was not written")
    staging.rename(output_dataset)
    return {
        "dataset": str(output_dataset),
        "session": SESSION,
        "preserved_passed": len(passed),
        "failed": len(failed),
        "routes": dict(Counter(entry["route"] for entry in entries)),
    }


def _record_and_base(dataset: Path, task_id: str):
    manifest, tasks = source_tasks(dataset)
    if task_id not in tasks:
        raise KeyError(task_id)
    return manifest, tasks[task_id], read_record(dataset, task_id)


def _save_manual_state(dataset: Path) -> None:
    scope = json.loads((Path(dataset) / "manual-scope.json").read_text())
    records = [read_record(dataset, task_id) for task_id in scope["failed_ids"]]
    write_json(
        Path(dataset) / "manual-state.json",
        {
            "session": SESSION,
            "phase": "completed"
            if all(r.get("manual_status") in {"verified", "blocked"} for r in records)
            else "running",
            "updated_at": utc_now(),
            "status_counts": dict(
                Counter(r.get("manual_status", "pending") for r in records)
            ),
            "route_counts": dict(Counter(r.get("manual_route") for r in records)),
            "manual_verified": sum(
                r.get("manual_status") == "verified" for r in records
            ),
            "manual_blocked": sum(r.get("manual_status") == "blocked" for r in records),
            "processed": sum(
                r.get("manual_status") in {"verified", "blocked"} for r in records
            ),
            "failed": len(records),
        },
    )


def manual_revise_stage(
    dataset: Path,
    task_id: str,
    stage_number: int,
    replacement: dict,
    reason: str,
    *,
    rewrite_requests: list[str] | None = None,
) -> dict:
    """Apply one complete local stage replacement and immediately verify it."""
    dataset = Path(dataset)
    if not 1 <= stage_number <= 2:
        raise ValueError("stage_number must be 1 or 2")
    manifest, base, record = _record_and_base(dataset, task_id)
    if record.get("manual_session") != SESSION:
        raise ValueError("Record is not part of this manual session")
    if record.get("manual_status") == "verified":
        raise ValueError("A manually verified record cannot be modified")
    old_candidate = record.get("draft")
    old_sha = digest(old_candidate) if old_candidate else None
    if record.get("manual_candidate_sha256") != old_sha:
        raise ValueError("Manual candidate hash is stale")
    requests = [
        q["request"] for q in record.get("question_plan", {}).get("requests", [])
    ]
    if len(requests) != 2:
        raise ValueError("Manual takeover requires two locked requests")
    new_requests = rewrite_requests or requests
    if len(new_requests) != 2 or any(len(value) < 12 for value in new_requests):
        raise ValueError("rewrite_requests must contain two substantive requests")
    draft = (
        SeriesDraft.model_validate(old_candidate)
        if old_candidate
        else SeriesDraft(base_task_id=task_id, base_task_sha256=digest(base), stages=[])
    )
    if draft.base_task_sha256 != digest(base):
        raise ValueError("Base task hash changed")
    stages = [stage.model_dump(mode="json") for stage in draft.stages]
    if rewrite_requests is not None:
        record["question_plan"]["requests"] = [
            {"stage": index + 1, "request": request}
            for index, request in enumerate(new_requests)
        ]
        record["question_audit"] = {
            "verdict": "manual_local_rewrite",
            "requests_sha256": digest(new_requests),
        }
        stages = stages[: stage_number - 1]
    while len(stages) < stage_number - 1:
        raise ValueError("Cannot create a later stage without its preceding stage")
    stages = stages[: stage_number - 1] + [
        StageDraft.model_validate(replacement).model_dump(mode="json")
    ]
    candidate = SeriesDraft(
        base_task_id=task_id,
        base_task_sha256=digest(base),
        stages=[StageDraft.model_validate(stage) for stage in stages],
    )
    verification, execution = verify_draft(
        base, Path(manifest["source_root"]), candidate
    )
    new_sha = digest(candidate)
    entry = {
        "task_id": task_id,
        "attempt": record.get("manual_attempt", 0) + 1,
        "affected_stage": stage_number,
        "change_kind": "question_and_candidate" if rewrite_requests else "candidate",
        "before_sha256": old_sha,
        "after_sha256": new_sha,
        "error_before": record.get("manual_last_error", ""),
        "reason": reason,
        "verification": {
            "valid": bool(verification.get("valid")),
            "replay_verified": bool(verification.get("replay_verified")),
            "implementation_sha256": pipeline.implementation_digest(),
        },
    }
    record["draft"] = candidate.model_dump(mode="json")
    record["manual_attempt"] = entry["attempt"]
    record["manual_candidate_sha256"] = new_sha
    record["manual_last_error"] = ""
    record["manual_history"].append(entry)
    record["verification"] = {
        **verification,
        "implementation_sha256": pipeline.implementation_digest(),
    }
    record["status"] = "verified" if len(candidate.stages) == 2 else "partial"
    record["manual_status"] = (
        "verified" if len(candidate.stages) == 2 else "in_progress"
    )
    record.pop("review", None)
    record.pop("validation_failure", None)
    record.pop("inflight_candidate", None)
    write_json(
        Path(dataset) / "journals" / f"{digest(task_id)}.json", execution.records
    )
    write_json(record_path(dataset, task_id), record)
    _save_manual_state(dataset)
    return {
        "task_id": task_id,
        "manual_status": record["manual_status"],
        "verification": entry["verification"],
    }


def mark_manual_blocked(
    dataset: Path, task_id: str, reason: str, *, infrastructure=False
) -> dict:
    """Persist a blocking reason without consuming a candidate modification."""
    _, _, record = _record_and_base(Path(dataset), task_id)
    record["manual_status"] = "blocked"
    record["manual_last_error"] = reason
    record["manual_block_kind"] = "infrastructure" if infrastructure else "content"
    record["manual_history"].append(
        {
            "task_id": task_id,
            "attempt": record.get("manual_attempt", 0),
            "change_kind": "infrastructure" if infrastructure else "candidate",
            "reason": reason,
            "verification": None,
        }
    )
    write_json(record_path(Path(dataset), task_id), record)
    _save_manual_state(Path(dataset))
    return {"task_id": task_id, "manual_status": "blocked"}


def next_tasks(dataset: Path, limit: int = 8) -> list[dict]:
    """Return unfinished tasks, prioritizing existing failed submissions."""
    if not 1 <= limit <= 30:
        raise ValueError("next task limit must be between 1 and 30")
    scope = json.loads((Path(dataset) / "manual-scope.json").read_text())
    records = [read_record(dataset, tid) for tid in scope["failed_ids"]]
    order = {
        "full_candidate": 0,
        "stage2_missing": 1,
        "stage1_missing": 2,
        "system_error": 3,
    }
    selected = [
        r for r in records if r.get("manual_status") not in {"verified", "blocked"}
    ]
    selected.sort(
        key=lambda r: (
            not bool(r.get("manual_pending_candidate")),
            order[r["manual_route"]],
            r["base_task_id"],
        )
    )
    return [
        {
            "task_id": r["base_task_id"],
            "route": r["manual_route"],
            "base_candidate_sha256": digest(
                r.get("manual_pending_candidate", r.get("draft"))
            ),
            "candidate_file": str(record_path(dataset, r["base_task_id"])),
            "error": str(
                r.get("manual_last_error")
                or r.get("review", {}).get("decision", {}).get("reasons", [])
            )[:1200],
        }
        for r in selected[:limit]
    ]


def current_status(dataset: Path) -> dict:
    """Count actual records and merge the preserved population by task ID."""
    dataset = Path(dataset)
    scope = json.loads((dataset / "manual-scope.json").read_text())
    parent_scope = json.loads(
        (Path(scope["source_dataset"]) / "scope.json").read_text()
    )
    inherited = set(parent_scope["passed_ids"]) | set(
        parent_scope["preserved_passed_repair_ids"]
    )
    preserved = inherited | set(scope["preserved_passed_ids"])
    remaining_ids = set(scope["failed_ids"])
    if preserved & remaining_ids or len(preserved | remaining_ids) != 800:
        raise ValueError("Coverage partition is not disjoint with denominator 800")
    records = [read_record(dataset, tid) for tid in sorted(remaining_ids)]
    accepted = [
        r["base_task_id"] for r in records if r.get("manual_status") == "verified"
    ]
    return {
        "session": SESSION,
        "updated_at": utc_now(),
        "expected_tasks": 800,
        "preserved_passed": len(preserved),
        "manual_verified": len(accepted),
        "total_passed": len(preserved | set(accepted)),
        "remaining": 800 - len(preserved | set(accepted)),
        "status_counts": dict(
            Counter(r.get("manual_status", "pending") for r in records)
        ),
        "route_counts": dict(
            Counter(
                r["manual_route"]
                for r in records
                if r.get("manual_status") != "verified"
            )
        ),
    }


def verify_submission(dataset: Path, submission: dict) -> dict:
    """Persist a personally authored candidate before strict local validation.

    The caller owns the dataset lock and ensures one worker per task. No author
    or judge API is used. A retry of the same pending content does not add a
    content attempt; stale submissions are rejected before any record is written.
    """
    from tau2.task_authoring.gaia2_series.questions import QuestionPlan

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    tid = submission["task_id"]
    scope = json.loads((dataset / "manual-scope.json").read_text())
    if tid not in scope["failed_ids"]:
        raise ValueError("Task is outside the manual repair scope")
    record = read_record(dataset, tid)
    if record.get("manual_status") == "verified":
        return {"task_id": tid, "status": "already_verified"}
    base_value = record.get("manual_pending_candidate", record.get("draft"))
    if submission["base_candidate_sha256"] != digest(base_value):
        raise ValueError("Stale base candidate hash")
    if not submission.get("reason", "").strip():
        raise ValueError("A concrete manual analysis reason is required")
    candidate_json = copy.deepcopy(submission["candidate"])
    plan = copy.deepcopy(submission.get("question_plan", record["question_plan"]))
    planned = QuestionPlan.model_validate(plan)
    requests = [q.request for q in planned.requests]
    draft = SeriesDraft.model_validate(candidate_json)
    if draft.base_task_id != tid or draft.base_task_sha256 != digest(tasks[tid]):
        raise ValueError("Candidate base task identity differs from sealed source")
    if len(draft.stages) != 2 or [s.request for s in draft.stages] != requests:
        raise ValueError(
            "Two candidate requests must match the submitted question plan"
        )
    changed = digest(candidate_json) != digest(base_value) or digest(plan) != digest(
        record.get("manual_pending_plan", record["question_plan"])
    )
    if changed:
        record["manual_attempt"] = record.get("manual_attempt", 0) + 1
    record["manual_pending_candidate"] = candidate_json
    record["manual_pending_plan"] = plan
    record["manual_status"] = "in_progress"
    record["manual_analysis"] = {
        "reason": submission["reason"],
        "candidate_sha256": digest(draft),
    }
    write_json(record_path(dataset, tid), record)
    entry = {
        "task_id": tid,
        "attempt": record.get("manual_attempt", 0),
        "before_sha256": digest(base_value),
        "after_sha256": digest(draft),
        "reason": submission["reason"],
        "started_at": utc_now(),
    }
    try:
        prefix = draft.model_copy(deep=True)
        prefix.stages = prefix.stages[:1]
        prefix_verification, _ = verify_draft(
            tasks[tid], Path(manifest["source_root"]), prefix
        )
        record["manual_verified_prefix"] = prefix.model_dump(mode="json")
        entry["stage_1_verification"] = prefix_verification
        verification, execution = verify_draft(
            tasks[tid], Path(manifest["source_root"]), draft
        )
        record.setdefault("manual_question_history", []).append(
            {"plan": record["question_plan"], "audit": record.get("question_audit")}
        )
        record.setdefault("revision_history", []).append(
            {
                "draft": record.get("draft"),
                "review": record.get("review"),
                "human": record.get("human"),
                "verification": record.get("verification"),
            }
        )
        verification.update(
            implementation_sha256=pipeline.implementation_digest(),
            manual_verifier_sha256=file_digest(Path(__file__)),
            requests_sha256=digest(requests),
            manual_session=SESSION,
        )
        record.update(
            draft=draft.model_dump(mode="json"),
            question_plan=plan,
            manual_candidate_sha256=digest(draft),
            verification=verification,
            manual_status="verified",
            status="manual_verified",
            manual_last_error="",
        )
        record["question_audit"] = {
            "verdict": "manual_local_analysis",
            "requests_sha256": digest(requests),
            "reason": submission["reason"],
        }
        record["manual_acceptance"] = {
            "method": "direct_analysis_and_local_strict_replay",
            "candidate_sha256": digest(draft),
            "requests_sha256": digest(requests),
            "session": SESSION,
            "reason": submission["reason"],
            "verified_at": utc_now(),
        }
        record.pop("review", None)
        record.pop("human", None)
        record.pop("manual_pending_candidate", None)
        record.pop("manual_pending_plan", None)
        write_json(dataset / "journals" / f"{digest(tid)}.json", execution.records)
        entry["verification"] = verification
    except Exception as error:
        record["manual_status"] = "pending"
        record["manual_last_error"] = f"{type(error).__name__}: {error}"
        entry["error"] = record["manual_last_error"]
    entry["finished_at"] = utc_now()
    record["manual_history"].append(entry)
    write_json(record_path(dataset, tid), record)
    return {
        "task_id": tid,
        "status": record["manual_status"],
        "error": record["manual_last_error"],
    }

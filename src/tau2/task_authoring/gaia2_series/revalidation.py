"""Immutable legacy snapshots, complete question audits and bounded rebuilds."""

import json
import shutil
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from tau2.config import (
    DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
    DEFAULT_GAIA2_ACCEPTANCE_TIMEOUT,
    DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY,
    DEFAULT_GAIA2_QUESTION_AUDIT_KEY_LIMITS,
    DEFAULT_GAIA2_QUESTION_AUDIT_PER_KEY_CONCURRENCY,
    DEFAULT_GAIA2_REVALIDATION_BATCH_SIZE,
    DEFAULT_GAIA2_REVALIDATION_CONCURRENCY,
)
from tau2.task_authoring.gaia2_series import pipeline
from tau2.task_authoring.gaia2_series.questions import (
    POLICY_VERSION,
    audit_questions,
    plan_questions,
)
from tau2.task_authoring.gaia2_series.schema import RequestPolicy
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    read_record,
    record_path,
    source_tasks,
    write_json,
    write_jsonl,
)

PILOT_ID = "0149_ev3wncotaounq2vooedt0t7v528hzvh5"
_report_lock = threading.Lock()


def utc_now():
    """Timestamp progress independently from the benchmark's scenario clock."""
    return datetime.now(timezone.utc).isoformat()


def default_policy(*, per_key_concurrency=1, key_limits=None, api_key_pool=None):
    """Conservative capacity; credentials remain private environment references."""
    return RequestPolicy(
        timeout_seconds=180,
        attempts=2,
        api_key_pool=(
            ["MIMO_API_KEY1", "MIMO_API_KEY3"] if api_key_pool is None else api_key_pool
        ),
        per_key_concurrency=per_key_concurrency,
        per_key_concurrency_overrides=key_limits or {},
        adaptive_concurrency=False,
    )


def prepare_version(source, output, config):
    """Snapshot the fixed legacy population and copy records without re-signing."""
    from tau2.task_authoring.gaia2_series.generation import resolve_profile

    source, output = Path(source).resolve(), Path(output).resolve()
    for profile in [config.author, config.reviewer]:
        if "mimo" not in resolve_profile(config, profile)[0].lower():
            raise ValueError("Revalidation uses MiMo only")
    if output.exists():
        pop = json.loads((output / "population.json").read_text())
        if (
            pop["source_dataset"] != str(source)
            or pop["policy_version"] != POLICY_VERSION
        ):
            raise ValueError("Existing output belongs to another source or policy")
        return pop
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("New version must be outside the legacy dataset")
    with pipeline.dataset_lock(source):
        manifest, tasks = source_tasks(source)
        records = {k: read_record(source, k) for k in tasks}
        passed = sorted(k for k, r in records.items() if r.get("status") == "reviewed")
        pop = {
            "source_dataset": str(source),
            "policy_version": POLICY_VERSION,
            "created_at": utc_now(),
            "expected_tasks": len(tasks),
            "legacy_reviewed": passed,
            "legacy_unresolved": sorted(set(tasks) - set(passed)),
            "records": {
                k: {
                    "status": r.get("status"),
                    "record_sha256": digest(r),
                    "file_sha256": file_digest(record_path(source, k)),
                    "content_sha256": digest(r.get("draft")),
                }
                for k, r in records.items()
            },
            "source_files": manifest["source_files"],
            "runtime_sha256": manifest["runtime_sha256"],
        }
        output.mkdir(parents=True)
        for name in ["work", "indexes", "journals"]:
            if (source / name).exists():
                shutil.copytree(source / name, output / name)
        for task_id, record in records.items():
            record["legacy_acceptance"] = {
                k: record.get(k) for k in ["status", "verification", "review", "human"]
            }
            write_json(record_path(output, task_id), record)
        write_json(
            output / "manifest.json",
            {
                **manifest,
                "parent_dataset": str(source),
                "question_policy": POLICY_VERSION,
                "run_kind": "question_revalidation",
                "execution_ready": False,
                "status": "authoring",
            },
        )
        write_json(output / "population.json", pop)
        write_json(output / "new-policy-config.json", config.model_dump(mode="json"))
        pipeline._configuration(output, config)
    return pop


class AuditCalls(list):
    """Persist each audit request without touching legacy work records."""

    def __init__(self, path):
        super().__init__(json.loads(path.read_text()) if path.exists() else [])
        self.path = path

    def append(self, item):
        super().append(item)
        write_json(self.path, self)


def _audit_one(dataset, config, task_id, tasks, source, *, legacy=True):
    from tau2.task_authoring.gaia2_series.generation import (
        config_identity,
        request_policy_override,
    )

    pop = json.loads((dataset / "population.json").read_text())
    origin = Path(pop["source_dataset"]) if legacy else dataset
    record = read_record(origin, task_id)
    if (
        legacy
        and file_digest(record_path(origin, task_id))
        != pop["records"][task_id]["file_sha256"]
    ):
        raise ValueError(f"Frozen source record changed: {task_id}")
    requests = [s["request"] for s in (record.get("draft") or {}).get("stages", [])]
    key = digest(
        {
            "policy": POLICY_VERSION,
            "requests": requests,
            "config": config_identity(config),
            "source": pop["source_files"],
            "runtime": pop["runtime_sha256"],
            "auditor_code": file_digest(Path(__file__).with_name("questions.py")),
        }
    )
    path = (
        dataset
        / ("question-audits" if legacy else "queue-audits")
        / f"{digest(task_id)}.json"
    )
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("audit_key") == key:
            return cached
    calls = AuditCalls(dataset / "question-audit-calls" / f"{digest(task_id)}.json")
    result = {
        "base_task_id": task_id,
        "audit_key": key,
        "policy_version": POLICY_VERSION,
        "requests_sha256": digest(requests),
        "content_sha256": digest(record.get("draft")),
        "started_at": utc_now(),
        "origin": "legacy" if legacy else "working",
    }
    token = request_policy_override.set(
        default_policy(
            per_key_concurrency=(
                DEFAULT_GAIA2_QUESTION_AUDIT_PER_KEY_CONCURRENCY if legacy else 1
            ),
            key_limits=DEFAULT_GAIA2_QUESTION_AUDIT_KEY_LIMITS if legacy else None,
        )
    )
    try:
        if len(requests) != 2:
            result.update(
                verdict="fail",
                affected_stages=[1, 2],
                reason="incomplete_user_request_series",
                attempts=[],
            )
        else:
            result.update(
                audit_questions(
                    dataset, tasks[task_id], source, requests, config, calls
                )
            )
    except Exception as error:
        result.update(
            verdict="error",
            affected_stages=[1, 2],
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        request_policy_override.reset(token)
    result.update(finished_at=utc_now(), model_requests=len(calls))
    write_json(path, result)
    return result


def audit_population(
    dataset, config, concurrency=DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY
):
    """Audit every frozen legacy-reviewed task; checkpoint counts after each result."""
    dataset = Path(dataset)
    if concurrency < 1:
        raise ValueError("Audit concurrency must be positive")
    manifest, tasks = source_tasks(dataset)
    pop = json.loads((dataset / "population.json").read_text())
    results = {}

    def publish():
        counts = Counter(r["verdict"] for r in results.values())
        report = {
            "policy_version": POLICY_VERSION,
            "expected": len(pop["legacy_reviewed"]),
            "recorded": len(results),
            "counts": dict(counts),
            "pending": len(pop["legacy_reviewed"]) - len(results),
            "complete": len(results) == len(pop["legacy_reviewed"])
            and not counts["error"],
            "updated_at": utc_now(),
            "dataset_complete": False,
            "release": False,
        }
        write_json(dataset / "question-audit-report.json", report)
        write_jsonl(
            dataset / "question-audits.jsonl", [results[k] for k in sorted(results)]
        )

    publish()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _audit_one, dataset, config, k, tasks, Path(manifest["source_root"])
            ): k
            for k in pop["legacy_reviewed"]
        }
        for future in as_completed(futures):
            key = futures[future]
            results[key] = future.result()
            publish()
            print(
                f"question_audit {len(results)}/{len(pop['legacy_reviewed'])} {key} {results[key]['verdict']}",
                flush=True,
            )
    return json.loads((dataset / "question-audit-report.json").read_text())


def build_queue(dataset):
    """Deduplicate old unresolved plus non-passing new audits, with provenance."""
    dataset = Path(dataset)
    pop = json.loads((dataset / "population.json").read_text())
    queue = {
        k: {
            "base_task_id": k,
            "origins": ["legacy_unresolved"],
            "route": "semantic_precheck",
        }
        for k in pop["legacy_unresolved"]
    }
    for k in pop["legacy_reviewed"]:
        path = dataset / "question-audits" / f"{digest(k)}.json"
        audit = (
            json.loads(path.read_text()) if path.exists() else {"verdict": "pending"}
        )
        if audit["verdict"] != "pass":
            item = queue.setdefault(k, {"base_task_id": k, "origins": []})
            item["origins"].append("question_audit")
            item.update(
                route="regenerate_questions"
                if audit["verdict"] == "fail"
                else "semantic_blocked",
                question_audit=audit,
            )
    for task_id, item in queue.items():
        record = read_record(dataset, task_id)
        item.update(
            content_sha256=digest(record.get("draft")),
            historical_attempts={
                k: record.get(k, {})
                for k in ["attempts", "repair_attempts", "recovery_attempts"]
            },
            new_policy_session=record.get("repair_sessions", {}).get(
                POLICY_VERSION, {}
            ),
            first_affected_stage=min(
                item.get("question_audit", {}).get("affected_stages") or [1]
            ),
        )
    write_json(
        dataset / "repair-queue.json",
        {
            "policy_version": POLICY_VERSION,
            "expected_tasks": pop["expected_tasks"],
            "count": len(queue),
            "tasks": [queue[k] for k in sorted(queue)],
        },
    )
    return queue


def _adopt(record, requests, audit, origin):
    record["question_plan"] = {
        "requests": [{"request": r} for r in requests],
        "origin": origin,
    }
    record["question_audit"] = audit


def rebuild_one(dataset, config, task_id):
    """Regenerate bad questions or patch technical errors, spending one shared budget."""
    from tau2.task_authoring.gaia2_series.generation import (
        config_identity,
        request_policy_override,
    )

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    record = read_record(dataset, task_id)
    if policy_complete(record):
        return {"base_task_id": task_id, "status": "accepted", "cached": True}
    session = POLICY_VERSION
    budget = record.setdefault("repair_sessions", {}).setdefault(session, {})
    budget.setdefault("attempts", {})
    for k in ["1", "2"]:
        budget["attempts"].setdefault(k, 0)
    record.setdefault("configuration_history", []).append(
        {
            "config_identity": record.get("config_identity"),
            "reason": "new_question_policy",
        }
    )
    record["config_identity"] = config_identity(config)
    record["calls"] = pipeline.PersistentCalls(record.get("calls", []), dataset, record)
    policy = default_policy().model_copy(
        update={"deadline_at": time.time() + DEFAULT_GAIA2_ACCEPTANCE_TIMEOUT}
    )
    token = request_policy_override.set(policy)
    result = {"base_task_id": task_id, "started_at": utc_now(), "status": "blocked"}
    try:
        audit = record.get("question_audit")
        saved_audit = dataset / "question-audits" / f"{digest(task_id)}.json"
        if not audit and saved_audit.exists():
            candidate_audit = json.loads(saved_audit.read_text())
            if candidate_audit.get("content_sha256") == digest(record.get("draft")):
                audit = candidate_audit
        if not audit:
            audit = _audit_one(
                dataset,
                config,
                task_id,
                tasks,
                Path(manifest["source_root"]),
                legacy=False,
            )
        old_requests = [
            s["request"] for s in (record.get("draft") or {}).get("stages", [])
        ]
        if audit["verdict"] == "pass":
            _adopt(record, old_requests, audit, "legacy_audited")
            if record.get("status") == "reviewed":
                record["status"] = "verified"
        elif audit["verdict"] == "fail":
            affected = min(audit.get("affected_stages") or [1])
            # A changed predecessor invalidates every later question and solution.
            retained = old_requests[: affected - 1]
            plan = None
            while (
                max(budget["attempts"][str(k)] for k in range(affected, 3))
                < DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS
            ):
                for k in range(affected, 3):
                    budget["attempts"][str(k)] += 1
                pipeline._persist(dataset, record)
                try:
                    plan = plan_questions(
                        dataset,
                        tasks[task_id],
                        Path(manifest["source_root"]),
                        config,
                        record["calls"],
                        failure_categories=["agent_reply_dependency_or_no_new_goal"],
                        retained_requests=retained,
                    )
                    requests = [q.request for q in plan.requests]
                    audited = audit_questions(
                        dataset,
                        tasks[task_id],
                        Path(manifest["source_root"]),
                        requests,
                        config,
                        record["calls"],
                    )
                    record.setdefault("question_plan_history", []).append(
                        {"plan": plan.model_dump(mode="json"), "audit": audited}
                    )
                    if audited["verdict"] == "pass":
                        for k in range(affected, 3):
                            budget["attempts"][str(k)] -= 1
                        break
                    plan = None
                except ValueError as error:
                    record.setdefault("question_plan_history", []).append(
                        {"error": str(error), "category": "invalid_question_plan"}
                    )
                    plan = None
                except Exception:
                    for k in range(affected, 3):
                        budget["attempts"][str(k)] -= 1
                    raise
            if plan is None:
                raise ValueError("Question planning budget exhausted")
            record.setdefault("revision_history", []).append(
                {
                    "draft": record.get("draft"),
                    "review": record.get("review"),
                    "human": record.get("human"),
                    "reason": "question_policy_rewrite",
                }
            )
            current = record.get("draft") or {
                "base_task_id": task_id,
                "base_task_sha256": digest(tasks[task_id]),
                "stages": [],
            }
            record["draft"] = {**current, "stages": current["stages"][: affected - 1]}
            record["question_plan"] = {
                **plan.model_dump(mode="json"),
                "origin": "source_isolated",
            }
            write_json(
                dataset / "question-plans" / f"{digest(task_id)}.json",
                record["question_plan"],
            )
            record["question_audit"] = audited
            record["status"] = "partial" if retained else "pending"
            for key in [
                "review",
                "human",
                "verification",
                "validation_failure",
                "recovery_stop",
            ]:
                record.pop(key, None)
        else:
            raise ValueError(f"Semantic audit unresolved: {audit['verdict']}")
        pipeline._persist(dataset, record)
        generation = pipeline.generate_dataset(
            dataset,
            config,
            [task_id],
            concurrency=1,
            request_policy=policy,
            repair=True,
            content_session=session,
            content_attempts=DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
        )
        record = read_record(dataset, task_id)
        if (
            record.get("status") != "verified"
            or len(record.get("draft", {}).get("stages", [])) != 2
        ):
            result.update(
                reason=record.get("last_error") or record.get("status"),
                generation=generation,
            )
        else:
            pipeline.verify_dataset(dataset, [task_id], workers=1)
            record = read_record(dataset, task_id)
            if record.get("status") == "verified":
                pipeline.review_dataset(
                    dataset,
                    config,
                    [task_id],
                    concurrency=1,
                    request_policy=policy,
                    write_sheets=False,
                )
            record = read_record(dataset, task_id)
            if record.get("status") == "reviewed":
                record["new_policy_complete"] = True
                result["status"] = "accepted"
            else:
                result["reason"] = record.get("last_error") or record.get("status")
        pipeline._persist(dataset, record)
    except Exception as error:
        result["reason"] = f"{type(error).__name__}: {error}"
        pipeline._persist(dataset, record)
    finally:
        request_policy_override.reset(token)
    result["finished_at"] = utc_now()
    write_json(dataset / "repair-results" / f"{digest(task_id)}.json", result)
    return result


def coverage(dataset):
    """Only policy + current technical verification + fresh review count as accepted."""
    dataset = Path(dataset)
    pop = json.loads((dataset / "population.json").read_text())
    accepted, blocked, pending = [], [], []
    for task_id in sorted(pop["records"]):
        r = read_record(dataset, task_id)
        if policy_complete(r):
            accepted.append(task_id)
        elif (dataset / "repair-results" / f"{digest(task_id)}.json").exists():
            blocked.append(task_id)
        else:
            pending.append(task_id)
    result = {
        "expected_tasks": pop["expected_tasks"],
        "accepted": len(accepted),
        "accepted_ids": accepted,
        "blocked": len(blocked),
        "blocked_ids": blocked,
        "pending": len(pending),
        "pending_ids": pending,
        "complete": len(accepted) == pop["expected_tasks"],
        "release": False,
        "execution_ready": False,
        "updated_at": utc_now(),
    }
    write_json(dataset / "new-policy-coverage.json", result)
    return result


def policy_complete(record):
    """Never trust a stale completion marker or legacy review alone."""
    draft = record.get("draft") or {}
    texts = [s.get("request") for s in draft.get("stages", [])]
    audit = record.get("question_audit", {})
    plan = record.get("question_plan", {})
    verification = record.get("verification", {})
    review = record.get("review", {})
    implementation = pipeline.implementation_digest()
    return bool(
        record.get("new_policy_complete")
        and record.get("status") == "reviewed"
        and len(texts) == 2
        and texts == [q.get("request") for q in plan.get("requests", [])]
        and audit.get("policy_version") == POLICY_VERSION
        and audit.get("verdict") == "pass"
        and audit.get("requests_sha256") == digest(texts)
        and verification.get("valid")
        and verification.get("content_sha256") == digest(draft)
        and verification.get("implementation_sha256") == implementation
        and review.get("accepted")
        and review.get("content_sha256") == digest(draft)
        and review.get("implementation_sha256") == implementation
    )


def run_workflow(
    dataset, config, *, audit_concurrency=DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY
):
    """Audit the frozen population, then repair only the deduplicated queue.

    The pilot is reported separately and does not block useful queue work.
    """
    dataset = Path(dataset)
    calibration = calibrate_questions(dataset, config)
    if not calibration["passed"]:
        report = {
            "stop_reason": "question_audit_calibration_failed",
            "calibration": calibration,
            "dataset_complete": False,
            "release": False,
            "updated_at": utc_now(),
        }
        write_json(dataset / "workflow.json", report)
        return report
    audit_population(dataset, config, concurrency=audit_concurrency)
    queue = build_queue(dataset)
    pilot = rebuild_one(dataset, config, PILOT_ID)
    ids = [k for k in sorted(queue) if k != PILOT_ID]
    empty_batches = 0
    ids = [k for k in ids if not policy_complete(read_record(dataset, k))]
    size = DEFAULT_GAIA2_REVALIDATION_BATCH_SIZE
    for offset in range(0, len(ids), size):
        batch = ids[offset : offset + size]
        with ThreadPoolExecutor(
            max_workers=DEFAULT_GAIA2_REVALIDATION_CONCURRENCY
        ) as pool:
            results = list(pool.map(lambda k: rebuild_one(dataset, config, k), batch))
        gained = sum(r["status"] == "accepted" and not r.get("cached") for r in results)
        empty_batches = empty_batches + 1 if not gained else 0
        write_json(
            dataset / "batches" / f"revalidation-{offset // size + 1}.json",
            {"results": results, "new_accepted": gained},
        )
        report = coverage(dataset)
        write_json(
            dataset / "workflow.json", {**report, "last_batch": offset // size + 1}
        )
        if empty_batches >= 2:
            report["stop_reason"] = "two_batches_without_progress"
            break
    report.update(
        pilot=pilot, repair_queue=len(queue), batch_scope=len(ids), pilot_blocking=False
    )
    write_json(dataset / "workflow.json", report)
    return report


def calibrate_questions(dataset, config):
    """Check explicit good/bad cases before spending the full legacy audit budget."""
    from tau2.task_authoring.gaia2_series.generation import request_policy_override

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    good = [
        "Regarding the Email List group requested earlier, use the final title Deleted Email Contacts and post a welcome message explaining its purpose for future email follow-up. Keep membership limited to reachable senders from the original request.",
        "Create a calendar reminder named Deleted Email Contacts follow-up on 16 October 2024 from 10:00 to 10:15 UTC. Put the requested group title in its description.",
    ]
    bad = [
        "Okay, go ahead: create the Email List group chat with just the deleted-email senders who are actually on Chats, and tell me who could not be added.",
        "Yes, use the person you just recommended and do exactly the option you suggested in your previous reply.",
    ]
    cases = [
        ("business_dependency", good, "pass"),
        ("courtesy_is_not_dependency", ["Thanks — " + s for s in good], "pass"),
        ("oracle_clarification_and_recommendation", bad, "fail"),
    ]
    key = digest(
        {
            "policy": POLICY_VERSION,
            "cases": cases,
            "config": config.model_dump(mode="json"),
            "code": file_digest(Path(__file__).with_name("questions.py")),
        }
    )
    path = dataset / "question-calibration.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("key") == key:
            return saved
    results = []
    token = request_policy_override.set(default_policy())
    try:
        for name, texts, expected in cases:
            calls = AuditCalls(dataset / "calibration-calls" / f"{name}.json")
            try:
                result = audit_questions(
                    dataset,
                    tasks[PILOT_ID],
                    Path(manifest["source_root"]),
                    texts,
                    config,
                    calls,
                )
                results.append(
                    {
                        "case": name,
                        "expected": expected,
                        "audit": result,
                        "passed": result["verdict"] == expected
                        and all(
                            stage["verdict"] == expected
                            for stage in result["attempts"][-1]["decision"]["stages"]
                        ),
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "case": name,
                        "expected": expected,
                        "passed": False,
                        "error": str(error),
                    }
                )
            print(
                f"question_calibration {name} passed={results[-1]['passed']}",
                flush=True,
            )
    finally:
        request_policy_override.reset(token)
    report = {
        "key": key,
        "passed": all(r["passed"] for r in results),
        "cases": results,
        "updated_at": utc_now(),
    }
    write_json(path, report)
    return report

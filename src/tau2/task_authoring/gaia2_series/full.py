"""Resumable full-population authoring with explicit human gates and bounded retries."""

from __future__ import annotations

import copy
import json
import math
import os
import random
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tau2.config import (
    DEFAULT_GAIA2_FULL_BATCH_SIZE,
    DEFAULT_GAIA2_FULL_PROGRESS_SECONDS,
    DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS,
)
from tau2.task_authoring.gaia2_series import pipeline
from tau2.task_authoring.gaia2_series.health import RequestHealth
from tau2.task_authoring.gaia2_series.schema import HumanDecision, RequestPolicy
from tau2.task_authoring.gaia2_series.storage import (
    CATEGORIES,
    digest,
    read_record,
    record_path,
    source_tasks,
    write_json,
    write_jsonl,
)


def preflight(dataset, config):
    """Validate pins and configuration without writing or making model calls."""
    from tau2.task_authoring.gaia2_series.generation import config_identity

    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    pipeline._check_open(manifest)
    saved = json.loads((dataset / "generation-config.json").read_text())
    if saved["identity"] != config_identity(config):
        raise ValueError("Generation configuration differs from the reviewed pilot")
    if manifest.get("human_review_policy") != "pilot_plus_anomalies":
        raise ValueError("The dataset does not have the approved full-run policy")
    for task_id in manifest["pilot_ids"]:
        record = read_record(dataset, task_id)
        human = record.get("human", {})
        if human.get("decision") != "accepted" or human.get("content_sha256") != digest(
            record.get("draft")
        ):
            raise ValueError(f"Missing or stale pilot approval: {task_id}")
    return {
        "expected_tasks": len(tasks),
        "preserved_pilots": manifest["pilot_ids"],
        "new_tasks": len(tasks) - len(manifest["pilot_ids"]),
        "runtime_sha256": manifest["runtime_sha256"],
        "implementation_sha256": pipeline.implementation_digest(),
        "model_calls": 0,
        "release": False,
    }


def utc_now():
    """Return an explicit UTC timestamp for persisted operational records."""
    return datetime.now(timezone.utc).isoformat()


def prepare_dataset(pilot: Path, output: Path, decisions: Path) -> dict:
    """Import only approved task records into an atomically initialized dataset."""
    pilot, output = Path(pilot).resolve(), Path(output).resolve()
    manifest, tasks = source_tasks(pilot)
    if output.exists() or output.is_relative_to(pilot) or pilot.is_relative_to(output):
        raise ValueError("Use a new output directory outside the pilot")
    approved = [
        HumanDecision.model_validate_json(line)
        for line in Path(decisions).read_text().splitlines()
        if line.strip()
    ]
    selected = sorted(value.base_task_id for value in approved)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Pilot decisions must be nonempty and unique")
    records = []
    for decision in approved:
        if decision.decision != "accepted" or decision.base_task_id not in tasks:
            raise ValueError("Only accepted source tasks can be migrated")
        record = read_record(pilot, decision.base_task_id)
        if (
            len((record.get("draft") or {}).get("stages", [])) != 2
            or digest(record["draft"]) != decision.content_sha256
            or not record.get("verification", {}).get("valid")
            or record.get("verification", {}).get("content_sha256")
            != decision.content_sha256
            or not record.get("review", {}).get("accepted")
            or record.get("review", {}).get("content_sha256") != decision.content_sha256
        ):
            raise ValueError(
                f"Pilot content or acceptance is stale: {decision.base_task_id}"
            )
        record["human"] = decision.model_dump(mode="json")
        records.append(record)
    staging = output.with_name(output.name + ".preparing")
    if staging.exists():
        raise ValueError(f"Incomplete preparation exists: {staging}")
    staging.mkdir(parents=True)
    for name in ["inventory.jsonl", "generation-config.json"]:
        shutil.copy2(pilot / name, staging / name)
    shutil.copytree(pilot / "indexes", staging / "indexes")
    for record in records:
        write_json(record_path(staging, record["base_task_id"]), record)
        journal = pilot / "journals" / f"{digest(record['base_task_id'])}.json"
        if journal.exists():
            (staging / "journals").mkdir(exist_ok=True)
            shutil.copy2(journal, staging / "journals" / journal.name)
    manifest = copy.deepcopy(manifest)
    manifest.update(
        parent_dataset=str(pilot),
        run_kind="full",
        pilot_ids=selected,
        additional_human_ids=[],
        human_review_ids=selected,
        human_review_policy="pilot_plus_anomalies",
        anomaly_policy="strict",
        status="authoring",
        execution_ready=False,
    )
    manifest.pop("release_sha256", None)
    write_json(staging / "manifest.json", manifest)
    write_json(
        staging / "migration.json",
        {
            "created_at": utc_now(),
            "parent_dataset": str(pilot),
            "imported_ids": selected,
            "expected_tasks": len(tasks),
            "human_decisions": [value.model_dump(mode="json") for value in approved],
        },
    )
    source_tasks(staging)
    staging.rename(output)
    return {
        "dataset": str(output),
        "preserved": selected,
        "remaining": len(tasks) - len(selected),
    }


def ordered_tasks(tasks, seed):
    """Interleave shuffled categories to avoid a category-biased warmup."""
    rng = random.Random(seed)
    groups = []
    for category in CATEGORIES:
        group = sorted(
            task_id
            for task_id, task in tasks.items()
            if task.scenario.category == category
        )
        rng.shuffle(group)
        groups.append(group)
    return [
        group[index]
        for index in range(max(map(len, groups)))
        for group in groups
        if index < len(group)
    ]


def automatic_errors(record, implementation):
    """Check automation independently of the later human-review gate."""
    clean = {key: value for key, value in record.items() if key != "human"}
    try:
        return pipeline.acceptance_errors(
            clean, human_required=False, implementation=implementation
        )
    except (ValueError, TypeError, KeyError) as error:
        return [f"invalid_record: {error}"]


def concurrency_for(current, maximum, *, calls, health_state=None, now=None, **unused):
    """Only completed provider requests influence concurrency, never task failures."""
    return RequestHealth(maximum, health_state).update(
        calls, current, time.time() if now is None else now
    )


def configure_provider_connections(concurrency):
    """Allocate enough synchronous HTTP connections for the selected model workers."""
    import httpx
    import litellm

    from tau2.utils import llm_utils  # noqa: F401

    connections = max(16, concurrency * 2)
    litellm.client_session = httpx.Client(
        limits=httpx.Limits(
            max_connections=connections, max_keepalive_connections=concurrency
        ),
        trust_env=False,
    )
    return connections


def run_full(
    dataset, config, *, start_concurrency, max_concurrency, request_policy=None
):
    """Drive all tasks to automatic review, stopping before human approval/release."""
    if not 1 <= start_concurrency <= max_concurrency <= 128:
        raise ValueError("Require 1 <= start concurrency <= max concurrency <= 128")
    configure_provider_connections(max_concurrency)
    dataset = Path(dataset)
    manifest, tasks = source_tasks(dataset)
    preflight(dataset, config)
    pipeline._check_open(manifest)
    if manifest.get("human_review_policy") != "pilot_plus_anomalies":
        raise ValueError(
            "Prepare a full dataset with the approved human-review policy first"
        )
    pipeline._configuration(dataset, config)
    implementation = pipeline.implementation_digest()
    policy = request_policy or RequestPolicy(timeout_seconds=600, attempts=2)
    state_path = dataset / "full-run.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {
            "created_at": utc_now(),
            "active_seconds": 0,
            "batches": 0,
            "implementation_sha256": implementation,
            "concurrency": start_concurrency,
            "start_concurrency": start_concurrency,
            "max_concurrency": max_concurrency,
            "request_policy": policy.model_dump(mode="json"),
            "provider_rounds": {},
        }
    )
    if state["implementation_sha256"] != implementation:
        raise ValueError("Full-run implementation changed; create a new version")
    if (
        state["start_concurrency"] != start_concurrency
        or state["max_concurrency"] != max_concurrency
    ):
        raise ValueError("Full-run concurrency bounds changed")
    if state["request_policy"] != policy.model_dump(mode="json"):
        raise ValueError("Full-run request policy changed")
    state.update(status="running", pid=os.getpid(), resumed_at=utc_now())
    state.setdefault("request_health", {})
    health_since = state["resumed_at"]
    started = time.monotonic()
    prior_seconds = state["active_seconds"]
    stop_monitor = threading.Event()
    lock = threading.Lock()
    progress_write_lock = threading.Lock()

    def records():
        return [read_record(dataset, task_id) for task_id in tasks]

    def progress():
        values = records()
        counts = Counter(value.get("status", "pending") for value in values)
        calls = [call for value in values for call in value.get("calls", [])]
        calls.sort(key=lambda value: value.get("started_at", ""))
        new_calls = [
            call for call in calls if call.get("started_at", "") >= state["created_at"]
        ]
        health_calls = [
            call for call in new_calls if call.get("started_at", "") >= health_since
        ]
        durations = sorted(
            call["elapsed_seconds"]
            for call in health_calls
            if call.get("elapsed_seconds") is not None
        )
        elapsed = prior_seconds + time.monotonic() - started
        complete = sum(not automatic_errors(value, implementation) for value in values)
        new_complete = sum(
            value["base_task_id"] not in manifest["pilot_ids"]
            and not automatic_errors(value, implementation)
            for value in values
        )
        generated = sum(
            len((value.get("draft") or {}).get("stages", [])) == 2 for value in values
        )
        new_generated = sum(
            value["base_task_id"] not in manifest["pilot_ids"]
            and len((value.get("draft") or {}).get("stages", [])) == 2
            for value in values
        )
        attempts = sum(sum(value.get("attempts", {}).values()) for value in values)
        failed_candidates = sum(
            not candidate.get("valid", True)
            for value in values
            for candidate in value.get("candidate_history", [])
        )
        with lock:
            current_phase = state.get("phase", "preflight")
            current_concurrency = state["concurrency"]
        report = {
            "updated_at": utc_now(),
            "pid": os.getpid(),
            "expected_tasks": len(tasks),
            "phase": current_phase,
            "concurrency": current_concurrency,
            "status_counts": dict(counts),
            "automatically_accepted": complete,
            "new_completed": new_complete,
            "generated": generated,
            "new_generated": new_generated,
            "candidate_validation_failure_rate": failed_candidates / max(1, attempts),
            "repair_attempts": sum(
                sum(value.get("repair_attempts", {}).values()) for value in values
            ),
            "anomaly_task_ids": [
                value["base_task_id"] for value in values if value.get("anomaly_flags")
            ],
            "elapsed_seconds": elapsed,
            "provider_error_rate": sum(bool(call.get("error")) for call in health_calls)
            / max(1, len(health_calls)),
            "capacity_error_rate": sum(
                bool(call.get("capacity_error")) for call in health_calls
            )
            / max(1, len(health_calls)),
            "request_mean_seconds": sum(durations) / len(durations)
            if durations
            else None,
            "request_p95_seconds": durations[math.ceil(0.95 * len(durations)) - 1]
            if durations
            else None,
            "generation_remaining_seconds_estimate": elapsed
            / new_generated
            * (len(tasks) - generated)
            if new_generated
            else None,
            "model_calls": len(new_calls),
            "release": False,
        }
        with progress_write_lock:
            write_json(dataset / "progress.json", report)
        health_calls = [
            call for call in new_calls if call.get("started_at", "") >= health_since
        ]
        return report, health_calls

    def monitor():
        while not stop_monitor.wait(DEFAULT_GAIA2_FULL_PROGRESS_SECONDS):
            try:
                report, _ = progress()
                print("full-progress " + json.dumps(report), flush=True)
            except Exception as error:
                print(
                    f"full-progress-error {type(error).__name__}: {error}", flush=True
                )

    def persist():
        state["active_seconds"] = prior_seconds + time.monotonic() - started
        state["updated_at"] = utc_now()
        write_json(state_path, state)

    last_health_check = float("-inf")

    def live_concurrency():
        """Apply request health inside a batch without cancelling in-flight tasks."""
        nonlocal last_health_check
        tick = time.monotonic()
        if tick - last_health_check < 30:
            return state["concurrency"]
        last_health_check = tick
        _, calls = progress()
        before = state["concurrency"]
        with lock:
            state["concurrency"] = (
                max_concurrency
                if not policy.adaptive_concurrency
                else concurrency_for(
                    before,
                    max_concurrency,
                    calls=calls,
                    health_state=state["request_health"],
                )
            )
        persist()
        write_json(
            dataset / "concurrency-decisions.json",
            state["request_health"].get("decisions", []),
        )
        if before != state["concurrency"]:
            print(f"live-concurrency: {before} -> {state['concurrency']}", flush=True)
            progress()
        return state["concurrency"]

    def phase(name, selected, function, **kwargs):
        for offset in range(0, len(selected), DEFAULT_GAIA2_FULL_BATCH_SIZE):
            if pipeline.implementation_digest() != implementation:
                raise ValueError("Authoring code changed during full generation")
            batch = selected[offset : offset + DEFAULT_GAIA2_FULL_BATCH_SIZE]
            with lock:
                state["phase"] = name
            persist()
            token = pipeline.batch_concurrency_control.set(
                (max_concurrency, live_concurrency)
            )
            try:
                report = function(
                    dataset,
                    config,
                    batch,
                    concurrency=state["concurrency"],
                    request_policy=policy,
                    **kwargs,
                )
            finally:
                pipeline.batch_concurrency_control.reset(token)
            state["batches"] += 1
            write_json(
                dataset / "batches" / f"{state['batches']:04d}-{name}.json", report
            )
            for item in report["results"]:
                if item["status"] not in {"reviewed", "verified"}:
                    record = read_record(dataset, item["id"])
                    record["anomaly_flags"] = sorted(
                        set(record.get("anomaly_flags", [])) | {item["status"]}
                    )
                    write_json(record_path(dataset, item["id"]), record)
            progress_report, calls = progress()
            with lock:
                state["concurrency"] = (
                    max_concurrency
                    if not policy.adaptive_concurrency
                    else concurrency_for(
                        state["concurrency"],
                        max_concurrency,
                        calls=calls,
                        health_state=state["request_health"],
                    )
                )
            persist()
            write_json(
                dataset / "concurrency-decisions.json",
                state["request_health"].get("decisions", []),
            )
            failures = [call for call in calls[-100:] if call.get("error")]
            remaining_cooldown = (
                state["request_health"].get("cooldown_until", 0) - time.time()
            )
            if remaining_cooldown > 0:
                time.sleep(min(60, remaining_cooldown))
            if any(not call.get("retryable", True) for call in failures):
                raise RuntimeError(
                    "Non-transient provider failure; inspect request records"
                )

    def incomplete():
        return [
            task_id
            for task_id in order
            if task_id not in manifest["pilot_ids"]
            and len(
                (read_record(dataset, task_id).get("draft") or {}).get("stages", [])
            )
            < 2
        ]

    order = ordered_tasks(tasks, manifest["seed"])
    write_json(
        dataset / "task-order.json", {"task_ids": order, "seed": manifest["seed"]}
    )
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    persist()
    try:
        progress()
        reclassified = [
            task_id
            for task_id in incomplete()
            if read_record(dataset, task_id).get("error_reclassification")
            and read_record(dataset, task_id).get("status") == "needs_revision"
        ]
        phase(
            "reclassified-repair", reclassified, pipeline.generate_dataset, repair=True
        )
        phase(
            "generation",
            [
                task_id
                for task_id in incomplete()
                if pipeline.normal_generation_available(
                    read_record(dataset, task_id), config.max_attempts
                )
                and (
                    read_record(dataset, task_id).get("status") != "provider_error"
                    or state["provider_rounds"].get(task_id, 0)
                    < DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS
                )
            ],
            pipeline.generate_dataset,
        )
        repair_ids = [
            task_id
            for task_id in incomplete()
            if read_record(dataset, task_id).get("status") == "needs_revision"
            or not pipeline.normal_generation_available(
                read_record(dataset, task_id), config.max_attempts
            )
        ]
        phase("repair", repair_ids, pipeline.generate_dataset, repair=True)
        for provider_round in range(DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS):
            retry_ids = [
                task_id
                for task_id in incomplete()
                if read_record(dataset, task_id).get("status") == "provider_error"
                and state["provider_rounds"].get(task_id, 0)
                < DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS
            ]
            if not retry_ids:
                break
            for task_id in retry_ids:
                state["provider_rounds"][task_id] = (
                    state["provider_rounds"].get(task_id, 0) + 1
                )
            persist()
            phase(
                f"provider-retry-{provider_round + 1}",
                retry_ids,
                pipeline.generate_dataset,
                repair=True,
            )
        state["phase"] = "verification"
        persist()
        verification = pipeline.verify_dataset(
            dataset, order, workers=start_concurrency
        )
        write_json(dataset / "full-verification-report.json", verification)
        complete_ids = [
            task_id
            for task_id in order
            if read_record(dataset, task_id).get("verification", {}).get("valid")
            and len(
                (read_record(dataset, task_id).get("draft") or {}).get("stages", [])
            )
            == 2
        ]
        phase(
            "review",
            [
                task_id
                for task_id in complete_ids
                if read_record(dataset, task_id).get("status") != "provider_error"
                or state["provider_rounds"].get("review:" + task_id, 0)
                < DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS
            ],
            pipeline.review_dataset,
        )
        for cycle in range(3):
            rejected = [
                task_id
                for task_id in complete_ids
                if read_record(dataset, task_id).get("review", {}).get("accepted")
                is False
                and task_id not in manifest["pilot_ids"]
            ]
            if not rejected:
                break
            phase(
                f"review-repair-{cycle + 1}",
                rejected,
                pipeline.generate_dataset,
                repair=True,
            )
            fixed = [
                task_id
                for task_id in rejected
                if read_record(dataset, task_id).get("status") == "verified"
            ]
            phase(f"rereview-{cycle + 1}", fixed, pipeline.review_dataset)
        for provider_round in range(DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS):
            retry_ids = [
                task_id
                for task_id in complete_ids
                if read_record(dataset, task_id).get("status") == "provider_error"
                and state["provider_rounds"].get("review:" + task_id, 0)
                < DEFAULT_GAIA2_FULL_PROVIDER_ROUNDS
            ]
            if not retry_ids:
                break
            for task_id in retry_ids:
                key = "review:" + task_id
                state["provider_rounds"][key] = state["provider_rounds"].get(key, 0) + 1
            persist()
            phase(
                f"review-provider-retry-{provider_round + 1}",
                retry_ids,
                pipeline.review_dataset,
            )
        required = pipeline.human_review_ids(dataset, manifest, tasks)
        anomalies = pipeline.anomaly_ids(dataset, tasks)
        write_json(
            dataset / "anomalies.json",
            {"task_ids": sorted(anomalies), "human_required": sorted(required)},
        )
        pipeline.review_sheet(dataset, order)
        write_jsonl(
            dataset / "anomaly-review-template.jsonl",
            (
                {
                    "base_task_id": task_id,
                    "content_sha256": digest(
                        read_record(dataset, task_id).get("draft")
                    ),
                    "decision": "PENDING",
                    "reviewer": "",
                }
                for task_id in sorted(anomalies)
            ),
        )
        remaining = {
            task_id: automatic_errors(read_record(dataset, task_id), implementation)
            for task_id in order
        }
        remaining = {task_id: errors for task_id, errors in remaining.items() if errors}
        write_json(
            dataset / "coverage.json",
            {
                "expected_tasks": len(tasks),
                "automatically_accepted": len(tasks) - len(remaining),
                "added_requests": sum(
                    len((value.get("draft") or {}).get("stages", []))
                    for value in records()
                ),
                "failures": remaining,
                "required_human_ids": sorted(required),
                "pending_human_ids": [
                    task_id
                    for task_id in sorted(required)
                    if pipeline.acceptance_errors(
                        read_record(dataset, task_id),
                        human_required=True,
                        implementation=implementation,
                    )
                ],
                "release": False,
                "execution_ready": False,
            },
        )
        state["status"] = "needs_attention" if remaining else "awaiting_human_review"
        state["automatically_accepted"] = len(tasks) - len(remaining)
        persist()
    except BaseException as error:
        state.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            error=f"{type(error).__name__}: {error}",
        )
        persist()
        raise
    finally:
        stop_monitor.set()
        monitor_thread.join()
        progress()
    return state

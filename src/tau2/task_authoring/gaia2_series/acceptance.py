"""Single-task acceptance using the production generation and review pipeline."""

import json
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tau2.config import (
    DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
    DEFAULT_GAIA2_ACCEPTANCE_TIMEOUT,
)
from tau2.task_authoring.gaia2_series import pipeline
from tau2.task_authoring.gaia2_series.schema import AuthoringConfig, RequestPolicy
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    read_record,
    record_path,
    source_tasks,
    write_json,
)


def _source_hashes(dataset: Path) -> dict:
    return {p.name: file_digest(p) for p in sorted((dataset / "work").glob("*.json"))}


def run_acceptance(
    dataset: Path,
    output: Path,
    task_id: str,
    config: AuthoringConfig,
    *,
    request_policy: RequestPolicy | None = None,
    timeout_seconds: float = DEFAULT_GAIA2_ACCEPTANCE_TIMEOUT,
    content_attempts: int = DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
) -> dict:
    """Copy one selected task, preserve provenance, and spend one bounded session.

    The denominator remains the sealed population. Success is scoped to this
    task only, never to the dataset or release/human-review gates.
    """
    from tau2.task_authoring.gaia2_series.generation import (
        config_identity,
        resolve_profile,
    )

    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if (
        output.exists()
        or output.is_relative_to(dataset)
        or dataset.is_relative_to(output)
    ):
        raise ValueError(
            "Acceptance requires a new output directory outside the source dataset"
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if content_attempts < 1 or content_attempts > 10:
        raise ValueError("content_attempts must be between 1 and 10")
    for profile in [config.author, config.reviewer]:
        if "mimo" not in resolve_profile(config, profile)[0].lower():
            raise ValueError("This acceptance workflow is restricted to MiMo profiles")
    before = _source_hashes(dataset)
    with pipeline.dataset_lock(dataset):
        manifest, tasks = source_tasks(dataset)
        pipeline._check_open(manifest)
        if task_id not in tasks:
            raise ValueError("Acceptance task is not in the source population")
        original = read_record(dataset, task_id)
        if original.get("status") == "reviewed":
            raise ValueError(
                "Acceptance selects an unresolved task, not an accepted record"
            )
        output.mkdir(parents=True)
        write_json(output / "original-record.json", original)
        write_json(
            output / "manifest.json", {**manifest, "parent_dataset": str(dataset)}
        )
        snapshot = tasks[task_id].scenario.snapshot
        (output / "indexes").mkdir()
        shutil.copy2(
            dataset / "indexes" / f"{snapshot}.json",
            output / "indexes" / f"{snapshot}.json",
        )
        journal = dataset / "journals" / f"{digest(task_id)}.json"
        if journal.exists():
            (output / "journals").mkdir()
            shutil.copy2(journal, output / "journals" / journal.name)
    session_id = "acceptance-v3"
    record = json.loads(json.dumps(original))
    record.setdefault("configuration_history", []).append(
        {
            "config_identity": record.get("config_identity"),
            "original_record_sha256": digest(original),
            "reason": "isolated_acceptance_with_new_submission_protocol",
        }
    )
    record["config_identity"] = config_identity(config)
    record.pop("recovery_stop", None)
    write_json(record_path(output, task_id), record)
    start_calls = len(record.get("calls", []))
    started = time.time()
    key_pool = ["MIMO_API_KEY1", "MIMO_API_KEY3"]
    if config.api_config_dir:
        from tau2.utils.api_config import get_api_configuration

        key_pool = get_api_configuration(config.api_config_dir).json_value(
            "REALBENCH_AUTHOR_KEY_POOL_JSON", []
        )
    policy = request_policy or RequestPolicy(
        timeout_seconds=180,
        attempts=2,
        api_key_pool=key_pool,
        per_key_concurrency=1,
        adaptive_concurrency=False,
    )
    policy = policy.model_copy(update={"deadline_at": started + timeout_seconds})
    report = {
        "task_id": task_id,
        "selected": 1,
        "expected_tasks": manifest["expected_tasks"],
        "session_id": session_id,
        "source_dataset": str(dataset),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "scope_complete": False,
        "dataset_complete": False,
        "release": False,
        "request_policy": policy.model_dump(mode="json"),
        "content_attempts": content_attempts,
        "implementation_sha256": pipeline.implementation_digest(),
    }

    def checkpoint(phase):
        report["phase"] = phase
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "acceptance-report.json", report)

    def deadline():
        if time.time() >= policy.deadline_at:
            raise TimeoutError("Acceptance wall-clock deadline reached")

    checkpoint("generation")
    try:
        with pipeline.dataset_lock(output):
            report["generation"] = pipeline.generate_dataset(
                output,
                config,
                [task_id],
                concurrency=1,
                request_policy=policy,
                repair=True,
                content_session=session_id,
                content_attempts=content_attempts,
            )
            record = read_record(output, task_id)
            if (
                record.get("status") == "verified"
                and len(record.get("draft", {}).get("stages", [])) == 2
            ):
                deadline()
                checkpoint("verification")
                report["verification"] = pipeline.verify_dataset(
                    output, [task_id], workers=1
                )
                record = read_record(output, task_id)
                if record.get("status") == "verified":
                    deadline()
                    checkpoint("review")
                    report["review"] = pipeline.review_dataset(
                        output, config, [task_id], concurrency=1, request_policy=policy
                    )
            record = read_record(output, task_id)
            report["scope_complete"] = (
                record.get("status") == "reviewed"
                and len(record.get("draft", {}).get("stages", [])) == 2
            )
            report["status"] = (
                "accepted" if report["scope_complete"] else "needs_attention"
            )
            report["stop_reason"] = (
                None
                if report["scope_complete"]
                else record.get("recovery_stop", {}).get("reason", record.get("status"))
            )
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        record = read_record(output, task_id)
        calls = record.get("calls", [])[start_calls:]
        write_json(output / "acceptance-calls.json", calls)
        report["elapsed_seconds"] = time.time() - started
        report["source_unchanged"] = before == _source_hashes(dataset)
        report["record_status"] = record.get("status")
        report["verified_new_stages"] = len(record.get("draft", {}).get("stages", []))
        report["session_budget"] = record.get("repair_sessions", {}).get(session_id, {})
        report["model_requests"] = len(calls)
        report["api_errors"] = sum(bool(c.get("error")) for c in calls)
        report["submissions"] = dict(
            Counter(
                t["name"]
                for c in calls
                for t in c.get("tool_calls", [])
                if t.get("name", "").startswith("submit_")
            )
        )
        report["last_error"] = record.get("last_error")
        report["validation_failure"] = record.get("validation_failure")
        if not report["source_unchanged"]:
            report.update(
                status="failed",
                scope_complete=False,
                error="Source work records changed during acceptance",
            )
        checkpoint("finished")
    return report

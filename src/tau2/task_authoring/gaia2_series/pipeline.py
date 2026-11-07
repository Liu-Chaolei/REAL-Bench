"""Resumable authoring, content-bound review gates, and explicit coverage exports."""

import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from contextvars import ContextVar
from difflib import SequenceMatcher
from pathlib import Path

from tau2.config import (
    DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
    DEFAULT_GAIA2_SERIES_STALL_REPEATS,
)
from tau2.task_authoring.gaia2_series import PROMPT_VERSION, VERSION
from tau2.task_authoring.gaia2_series.compiler import REFERENCE, compile_series
from tau2.task_authoring.gaia2_series.failures import (
    CandidateValidationError,
    VerificationInfrastructureError,
)
from tau2.task_authoring.gaia2_series.materials import context_packet
from tau2.task_authoring.gaia2_series.repair import failure_feedback
from tau2.task_authoring.gaia2_series.schema import (
    HumanDecision,
    RequestPolicy,
    ReviewDecision,
    SeriesDraft,
    StageDraft,
)
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    read_record,
    record_path,
    source_tasks,
    stratified_ids,
    write_json,
    write_jsonl,
)
from tau2.task_authoring.gaia2_series.verification import (
    execute_reference,
    verify_draft,
)

batch_concurrency_control = ContextVar(
    "gaia2_series_batch_concurrency_control", default=None
)


@contextmanager
def dataset_lock(dataset: Path):
    """Reject simultaneous mutating commands instead of losing review records."""
    import fcntl

    with (Path(dataset) / ".authoring.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another authoring command owns this dataset") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def implementation_digest() -> str:
    """Bind verification/review to the exact authoring implementation."""
    return digest(
        {p.name: file_digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
    )


def selected_ids(manifest, tasks, *, ids=None, pilot=False, per_category=None):
    """Select an explicit scope; full coverage always retains the 800 denominator."""
    if sum([ids is not None, pilot, per_category is not None]) > 1:
        raise ValueError("Choose task IDs, pilot, or per-category sampling")
    if ids is not None:
        if not ids or len(ids) != len(set(ids)) or set(ids) - tasks.keys():
            raise ValueError(
                "Task IDs must be nonempty, unique, and in the source population"
            )
        return sorted(ids)
    if pilot:
        return manifest["pilot_ids"]
    if per_category is not None:
        if per_category < 1:
            raise ValueError("per_category must be positive")
        return stratified_ids(tasks, per_category, manifest["seed"])
    return sorted(tasks)


def anomaly_ids(dataset: Path, tasks: dict) -> set[str]:
    """Flag retries, repairs, rejections, boundary warnings, and duplicates."""
    flagged = set()
    for task_id in tasks:
        record = read_record(dataset, task_id)
        if (
            record.get("revision_history")
            or any(record.get("repair_attempts", {}).values())
            or record.get("anomaly_flags")
            or record.get("status")
            in {
                "needs_revision",
                "provider_error",
                "verification_error",
                "review_error",
            }
        ):
            flagged.add(task_id)
        if any(
            entry.get("repair") or not entry.get("valid", True)
            for entry in record.get("candidate_history", [])
        ):
            flagged.add(task_id)
        if any(
            call.get("provider_attempt", 1) > 1 or call.get("error")
            for call in record.get("calls", [])
        ):
            flagged.add(task_id)
        review = record.get("review", {})
        if review and (
            not review.get("accepted", False)
            or review.get("decision", {}).get("material_warnings")
        ):
            flagged.add(task_id)
    for group in duplicate_groups(dataset, tasks):
        flagged.update(group)
    flagged.update(
        task_id for group in similar_groups(dataset, tasks) for task_id in group
    )
    return flagged


def human_review_ids(dataset: Path, manifest: dict, tasks: dict) -> set[str]:
    """Return configured human pilots plus all automatically detected anomalies."""
    configured = set(manifest.get("human_review_ids", []))
    if not configured:
        configured.update(manifest.get("pilot_ids", []))
        configured.update(manifest.get("additional_human_ids", []))
    return configured | anomaly_ids(dataset, tasks)


def _persist(dataset, record):
    status = record.get("status")
    if status == "needs_revision":
        record["failure_class"] = "candidate"
    elif status in {"provider_error", "verification_error", "review_error"}:
        record["failure_class"] = "system"
    elif status in {"verified", "reviewed", "partial"}:
        record.pop("failure_class", None)
    flags = set(record.get("anomaly_flags", []))
    calls = record.get("calls", [])
    if calls and (calls[-1].get("error") or calls[-1].get("provider_attempt", 1) > 1):
        flags.add("provider_retry")
    if calls and calls[-1].get("empty_reply"):
        flags.add("empty_model_reply")
    history = record.get("candidate_history", [])
    if history and (
        history[-1].get("repair_metadata") or not history[-1].get("valid", True)
    ):
        flags.add("candidate_repair")
    if status in {
        "needs_revision",
        "provider_error",
        "verification_error",
        "review_error",
    }:
        flags.add(status)
    if flags:
        record["anomaly_flags"] = sorted(flags)
    write_json(record_path(dataset, record["base_task_id"]), record)


def _prefix_digest(stages):
    return digest([stage.model_dump(mode="json") for stage in stages])


class PersistentCalls(list):
    """Persist usage after every call, including calls before a cancelled stage."""

    def __init__(self, values, dataset, record):
        super().__init__(values)
        self.dataset, self.record = dataset, record

    def append(self, value):
        super().append(value)
        _persist(self.dataset, self.record)


def _check_open(manifest):
    if manifest.get("status") == "frozen":
        raise ValueError("Dataset is frozen; copy it to a new version before authoring")


def _configuration(dataset, config):
    from tau2.task_authoring.gaia2_series.generation import config_identity

    identity = config_identity(config)
    path = Path(dataset) / "generation-config.json"
    value = {
        "identity": identity,
        "config": config.model_dump(mode="json"),
        "prompt_version": PROMPT_VERSION,
    }
    if path.exists():
        if json.loads(path.read_text())["identity"] != identity:
            raise ValueError(
                "Generation configuration changed; use a new dataset version"
            )
        return identity
    write_json(path, value)
    return identity


def normal_generation_available(record: dict, max_attempts: int) -> bool:
    """Decide whether the next unfinished stage has normal attempts before replay."""
    number = len((record.get("draft") or {}).get("stages", [])) + 1
    return number <= 2 and record.get("attempts", {}).get(str(number), 0) < max_attempts


def rejected_stage(record: dict) -> int | None:
    """Locate the first rejected stage, only for the exact reviewed content."""
    draft = record.get("draft")
    if not draft:
        return None
    fingerprint = digest(draft)
    review, human = record.get("review", {}), record.get("human", {})
    if (
        review.get("content_sha256") == fingerprint and not review.get("accepted", True)
    ) or (
        human.get("content_sha256") == fingerprint
        and human.get("decision") == "rejected"
    ):
        return min(review.get("decision", {}).get("affected_stages") or [1])
    return None


def generation_stop_reason(
    record: dict,
    max_attempts: int,
    *,
    repair=False,
    recovery_rounds=0,
    content_session: str | None = None,
) -> str | None:
    """Check persisted content budgets and repeated errors before any replay.

    Count identical validation errors for the current stage and verified prefix,
    stopping at its last successful candidate. API failures and the generic review
    rejection marker are not content-validation attempts.
    """
    stages = (record.get("draft") or {}).get("stages", [])
    number = rejected_stage(record) or len(stages) + 1
    if number > 2:
        return None
    key, prefix = str(number), digest(stages[: number - 1])
    failed_candidate = False
    errors = {}
    newest_failure = None
    consecutive_identical = 0
    for entry in reversed(record.get("candidate_history", [])):
        if content_session and entry.get("content_session") != content_session:
            continue
        if entry.get("stage") != number or entry.get("prefix_sha256", prefix) != prefix:
            continue
        if entry.get("valid"):
            break
        failed_candidate = failed_candidate or bool(entry.get("candidate"))
        if entry.get("review_rejected"):
            continue
        if content_session:
            feedback = entry.get("feedback", {})
            marker = digest(
                {
                    "candidate": entry.get("candidate"),
                    "error": {
                        k: feedback.get(k)
                        for k in [
                            "phase",
                            "kind",
                            "pointer",
                            "reference",
                            "actual",
                            "expected",
                        ]
                    },
                    "message": feedback.get("message", entry.get("error")),
                }
            )
            if newest_failure is None:
                newest_failure = marker
            if marker != newest_failure:
                break
            consecutive_identical += 1
            if consecutive_identical >= DEFAULT_GAIA2_SERIES_STALL_REPEATS:
                return "identical_candidate_failure_stalled"
        error = entry.get("error")
        if error:
            errors[error] = errors.get(error, 0) + 1
            if errors[error] >= max_attempts:
                return "repeated_candidate_failure"
    budget = (
        record.get("repair_sessions", {}).get(content_session, {})
        if content_session
        else record
    )
    normal = budget.get("attempts", {}).get(key, 0)
    repairs = budget.get("repair_attempts", {}).get(key, 0)
    if not repair:
        return "normal_attempt_budget_exhausted" if normal >= max_attempts else None
    base_budget = max_attempts * (2 if failed_candidate and not content_session else 1)
    recovery = budget.get("recovery_attempts", {}).get(key, 0)
    if normal + repairs >= base_budget and recovery >= max_attempts * recovery_rounds:
        return "content_attempt_budget_exhausted"
    return None


def stop_generation(dataset: Path, record: dict, reason: str) -> dict:
    """Keep stalled candidates and counters intact for targeted intervention."""
    number = (
        rejected_stage(record) or len((record.get("draft") or {}).get("stages", [])) + 1
    )
    record["recovery_stop"] = {"reason": reason, "stage": number}
    if record.get("status") != "partial":
        record["status"] = "needs_revision"
    _persist(dataset, record)
    return {
        "id": record["base_task_id"],
        "status": record["status"],
        "skip_reason": reason,
    }


def generate_dataset(
    dataset: Path,
    config,
    task_ids: list[str],
    *,
    concurrency: int | None = None,
    request_policy: RequestPolicy | None = None,
    repair: bool = False,
    recovery_rounds: int = 0,
    content_session: str | None = None,
    content_attempts: int | None = None,
) -> dict:
    """Write checked continuations, isolating exhausted provider failures by task."""
    from tau2.task_authoring.gaia2_series.generation import (
        ProviderQuotaExhausted,
        ProviderUnavailable,
        author_stage,
        cancel_requested,
        repair_stage,
        request_policy_override,
        resolve_profile,
        sanitize,
    )

    manifest, tasks = source_tasks(dataset)
    _check_open(manifest)
    identity = _configuration(dataset, config)
    source = Path(manifest["source_root"])
    stop = threading.Event()
    policy = request_policy or RequestPolicy()
    workers = config.concurrency if concurrency is None else concurrency
    if workers < 1:
        raise ValueError("concurrency must be positive")
    if recovery_rounds < 0:
        raise ValueError("recovery_rounds must be nonnegative")
    if content_attempts is not None and (
        not content_session or not 1 <= content_attempts <= 10
    ):
        raise ValueError(
            "content_attempts requires a session and must be between 1 and 10"
        )

    def worker(task_id):
        from tau2.task_authoring.gaia2_series.questions import locked_requests

        cancel_requested.set(stop)
        request_policy_override.set(policy)
        if stop.is_set():
            return {"id": task_id, "status": "not_attempted_provider_unavailable"}
        base = tasks[task_id]
        record = read_record(dataset, task_id)
        planned = record.get("question_plan", {}).get("requests", [])
        requests = [q["request"] for q in planned] if len(planned) == 2 else None
        locked_requests.set(requests)
        if manifest.get("question_policy") and (
            not requests or record.get("question_audit", {}).get("verdict") != "pass"
        ):
            return {"id": task_id, "status": "needs_question_audit"}
        if content_session:
            budget = record.setdefault("repair_sessions", {}).setdefault(
                content_session, {}
            )
            for bucket in ["attempts", "repair_attempts", "recovery_attempts"]:
                budget.setdefault(bucket, {})
        else:
            budget = record
        limit = (
            content_attempts or DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS
            if content_session
            else config.max_attempts
        )
        if record.get("config_identity", identity) != identity:
            raise ValueError(
                "Work record was written with another generation configuration"
            )
        draft = (
            SeriesDraft.model_validate(record["draft"])
            if record.get("draft")
            else SeriesDraft(base_task_id=task_id, base_task_sha256=digest(base))
        )
        reason = generation_stop_reason(
            record,
            limit,
            repair=repair,
            recovery_rounds=recovery_rounds,
            content_session=content_session,
        )
        if reason:
            return {
                **stop_generation(dataset, record, reason),
                "skipped_before_replay": True,
            }
        record.pop("recovery_stop", None)
        review = record.get("review", {})
        human = record.get("human", {})
        rejected = review.get("content_sha256") == digest(draft) and not review.get(
            "accepted", True
        )
        rejected = rejected or (
            human.get("content_sha256") == digest(draft)
            and human.get("decision") == "rejected"
        )
        if rejected:
            record.setdefault("revision_history", []).append(
                {
                    "draft": draft.model_dump(mode="json"),
                    "review": review,
                    "human": human,
                }
            )
            affected = review.get("decision", {}).get("affected_stages") or [1]
            first_affected = min(affected)
            for number, old_stage in enumerate(draft.stages, 1):
                if number >= first_affected:
                    record.setdefault("candidate_history", []).append(
                        {
                            "stage": number,
                            "valid": False,
                            "candidate": old_stage.model_dump(mode="json"),
                            "error": "Independent or human review rejected this candidate",
                            "review_rejected": True,
                            "prefix_sha256": _prefix_digest(draft.stages[: number - 1]),
                        }
                    )
            draft.stages = draft.stages[: first_affected - 1]
            record.update(
                draft=draft.model_dump(mode="json"),
                last_error=json.dumps({"model_review": review, "human_review": human}),
            )
            for key in ["verification", "review", "human", "validation_failure"]:
                record.pop(key, None)
        # Revalidate resumed content and regenerate all later state from source.
        try:
            execution = execute_reference(base, source, draft if draft.stages else None)
        except Exception as error:
            _, _, secrets = resolve_profile(config, config.author)
            record.update(
                status="verification_error",
                last_error=sanitize(f"{type(error).__name__}: {error}", secrets),
            )
            _persist(dataset, record)
            return {"id": task_id, "status": "verification_error"}
        record.update(
            config_identity=identity,
            calls=record.get("calls", []),
            attempts=record.get("attempts", {}),
            repair_attempts=record.get("repair_attempts", {}),
        )
        record["calls"] = PersistentCalls(record["calls"], dataset, record)

        def feedback_for(error):
            _, _, secrets = resolve_profile(config, config.author)
            details = (
                error.details
                if isinstance(error, CandidateValidationError)
                else {"kind": "candidate_validation_failure", "phase": "generation"}
            )
            return json.loads(
                sanitize(
                    json.dumps(
                        {
                            **details,
                            "task_id": task_id,
                            "stage": len(draft.stages) + 1,
                            "message": f"{type(error).__name__}: {error}",
                        },
                        ensure_ascii=False,
                    ),
                    secrets,
                )
            )

        def accept(candidate, verification, next_execution):
            record.update(
                draft=candidate.model_dump(mode="json"),
                verification={
                    **verification,
                    "implementation_sha256": implementation_digest(),
                },
                status="verified" if len(candidate.stages) == 2 else "partial",
                last_error="",
            )
            record.pop("review", None)
            record.pop("human", None)
            record.pop("validation_failure", None)
            record.pop("inflight_candidate", None)
            write_json(
                Path(dataset) / "journals" / f"{digest(task_id)}.json",
                next_execution.records,
            )
            _persist(dataset, record)

        while len(draft.stages) < 2 and not stop.is_set():
            number = len(draft.stages) + 1
            key = str(number)
            feedback = record.get("validation_failure") or record.get("last_error", "")
            failed_entry = next(
                (
                    entry
                    for entry in reversed(record.get("candidate_history", []))
                    if entry.get("stage") == number
                    and not entry.get("valid")
                    and entry.get("candidate")
                    and (
                        not requests
                        or entry["candidate"].get("request") == requests[number - 1]
                    )
                    and entry.get("prefix_sha256", _prefix_digest(draft.stages))
                    == _prefix_digest(draft.stages)
                ),
                None,
            )
            failed_candidate = failed_entry["candidate"] if failed_entry else None
            if repair and failed_candidate and not failed_entry.get("review_rejected"):
                resumed = draft.model_copy(deep=True)
                try:
                    resumed.stages.append(StageDraft.model_validate(failed_candidate))
                    verification, next_execution = verify_draft(base, source, resumed)
                    record.setdefault("candidate_history", []).append(
                        {
                            "stage": number,
                            "candidate": failed_candidate,
                            "valid": True,
                            "recovered_without_model": True,
                        }
                    )
                    accept(resumed, verification, next_execution)
                    draft, execution = resumed, next_execution
                    continue
                except (CandidateValidationError, ValueError) as error:
                    feedback = {
                        **record.get("validation_failure", {}),
                        **feedback_for(error),
                    }
                    record["validation_failure"] = feedback
                    _persist(dataset, record)
                except Exception as error:
                    _, _, secrets = resolve_profile(config, config.author)
                    record.update(
                        status="verification_error",
                        last_error=sanitize(
                            f"{type(error).__name__}: {error}", secrets
                        ),
                    )
                    _persist(dataset, record)
                    return {"id": task_id, "status": "verification_error"}
            normal_attempts = budget["attempts"].get(key, 0)
            repair_attempts = budget["repair_attempts"].get(key, 0)
            recovery_attempts = budget.setdefault("recovery_attempts", {}).get(key, 0)
            stage_completed = False
            while True:
                if stop.is_set():
                    return {
                        "id": task_id,
                        "status": "not_attempted_provider_unavailable",
                    }
                reason = generation_stop_reason(
                    record,
                    limit,
                    repair=repair,
                    recovery_rounds=recovery_rounds,
                    content_session=content_session,
                )
                if reason:
                    return stop_generation(dataset, record, reason)
                base_budget = limit + (
                    limit if repair and failed_candidate and not content_session else 0
                )
                recovery_budget = limit * recovery_rounds
                if normal_attempts + repair_attempts < base_budget:
                    is_recovery = False
                    is_repair = repair_attempts > 0 or normal_attempts >= limit
                elif recovery_attempts < recovery_budget:
                    is_recovery = True
                    is_repair = True
                else:
                    break
                if is_recovery:
                    recovery_attempts += 1
                    budget["recovery_attempts"][key] = recovery_attempts
                    attempt = normal_attempts + repair_attempts + recovery_attempts
                elif is_repair:
                    repair_attempts += 1
                    budget["repair_attempts"][key] = repair_attempts
                    attempt = normal_attempts + repair_attempts
                else:
                    normal_attempts += 1
                    budget["attempts"][key] = normal_attempts
                    attempt = normal_attempts
                record["status"] = "generating"
                _persist(dataset, record)
                stage = None
                repair_metadata = None
                try:
                    context = context_packet(
                        dataset,
                        base,
                        execution.states[number - 1],
                        config.context_characters,
                    )
                    if failed_candidate:
                        repair_result = repair_stage(
                            config,
                            base,
                            source,
                            execution,
                            context,
                            draft,
                            failed_candidate,
                            feedback,
                            record["calls"],
                        )
                        stage = repair_result.stage
                        repair_metadata = repair_result.model_dump(exclude={"stage"})
                    else:
                        stage = author_stage(
                            config,
                            base,
                            source,
                            execution,
                            context,
                            draft,
                            feedback,
                            record["calls"],
                        )
                    candidate = draft.model_copy(deep=True)
                    candidate.stages.append(stage)
                    record["inflight_candidate"] = {
                        "stage": number,
                        "candidate": stage.model_dump(mode="json"),
                        "prefix_sha256": _prefix_digest(draft.stages),
                    }
                    _persist(dataset, record)
                    verification, next_execution = verify_draft(base, source, candidate)
                    record.setdefault("candidate_history", []).append(
                        {
                            "stage": number,
                            "attempt": attempt,
                            "content_session": content_session,
                            "repair": is_repair,
                            "recovery": is_recovery,
                            "repair_metadata": repair_metadata,
                            "candidate": stage.model_dump(mode="json"),
                            "valid": True,
                        }
                    )
                    accept(candidate, verification, next_execution)
                    draft, execution = candidate, next_execution
                    stage_completed = True
                    break
                except ProviderUnavailable as error:
                    # Provider failures do not spend a content-quality attempt.
                    if is_recovery:
                        budget["recovery_attempts"][key] -= 1
                    elif is_repair:
                        budget["repair_attempts"][key] -= 1
                    else:
                        budget["attempts"][key] -= 1
                    record.update(status="provider_error", last_error=str(error))
                    _persist(dataset, record)
                    if policy.stop_batch_on_error or isinstance(
                        error, ProviderQuotaExhausted
                    ):
                        stop.set()
                    return {
                        "id": task_id,
                        "status": "provider_error",
                        "error": str(error),
                    }
                except VerificationInfrastructureError as error:
                    if is_recovery:
                        budget["recovery_attempts"][key] -= 1
                    elif is_repair:
                        budget["repair_attempts"][key] -= 1
                    else:
                        budget["attempts"][key] -= 1
                    _, _, secrets = resolve_profile(config, config.author)
                    record.update(
                        status="verification_error",
                        last_error=sanitize(
                            f"{type(error).__name__}: {error}", secrets
                        ),
                    )
                    _persist(dataset, record)
                    return {"id": task_id, "status": "verification_error"}
                except (CandidateValidationError, ValueError) as error:
                    _, _, secrets = resolve_profile(config, config.author)
                    feedback = sanitize(f"{type(error).__name__}: {error}", secrets)
                    candidate_json = (
                        stage.model_dump(mode="json") if stage else failed_candidate
                    )
                    details, candidate_json = failure_feedback(
                        error, candidate_json, record["calls"]
                    )
                    if (
                        requests
                        and candidate_json
                        and candidate_json.get("request") != requests[number - 1]
                    ):
                        candidate_json = None
                    details.update(task_id=task_id, stage=number)
                    details = json.loads(
                        sanitize(json.dumps(details, ensure_ascii=False), secrets)
                    )
                    raw_reply = details.get("raw_reply")
                    record.setdefault("candidate_history", []).append(
                        {
                            "stage": number,
                            "attempt": attempt,
                            "content_session": content_session,
                            "valid": False,
                            "error": feedback,
                            "feedback": details,
                            "repair": is_repair,
                            "recovery": is_recovery,
                            "repair_metadata": repair_metadata,
                            "candidate": candidate_json,
                            "raw_reply": raw_reply if stage is None else None,
                            "prefix_sha256": _prefix_digest(draft.stages),
                        }
                    )
                    record.update(
                        status="needs_revision",
                        last_error=feedback,
                        validation_failure=details,
                    )
                    _persist(dataset, record)
                    feedback, failed_candidate = details, candidate_json
                    normal_attempts = budget["attempts"].get(key, 0)
                    repair_attempts = budget["repair_attempts"].get(key, 0)
                    recovery_attempts = budget["recovery_attempts"].get(key, 0)
                except Exception as error:
                    bucket = (
                        "recovery_attempts"
                        if is_recovery
                        else "repair_attempts"
                        if is_repair
                        else "attempts"
                    )
                    budget[bucket][key] -= 1
                    _, _, secrets = resolve_profile(config, config.author)
                    record.update(
                        status="verification_error",
                        last_error=sanitize(
                            f"{type(error).__name__}: {error}", secrets
                        ),
                    )
                    _persist(dataset, record)
                    return {"id": task_id, "status": "verification_error"}
            if not stage_completed:
                return {"id": task_id, "status": "needs_revision", "error": feedback}
        if len(draft.stages) != 2:
            return {"id": task_id, "status": "not_attempted_provider_unavailable"}
        return {"id": task_id, "status": record.get("status", "verified")}

    report = _batch(dataset, task_ids, worker, workers, "generation")
    report.update(
        concurrency=workers,
        repair=repair,
        request_policy=policy.model_dump(mode="json"),
    )
    write_json(Path(dataset) / "generation-report.json", report)
    return report


def _batch(dataset, task_ids, worker, workers, name):
    implementation = implementation_digest()
    results = []
    control = batch_concurrency_control.get()
    maximum = control[0] if control else workers
    pending = iter(task_ids)
    exhausted = False
    with ThreadPoolExecutor(max_workers=maximum) as pool:
        futures = {}
        while futures or not exhausted:
            target = max(1, min(maximum, control[1]())) if control else workers
            while not exhausted and len(futures) < target:
                try:
                    task_id = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                futures[pool.submit(worker, task_id)] = task_id
            if not futures:
                break
            completed, _ = wait(futures, timeout=15, return_when=FIRST_COMPLETED)
            for future in completed:
                task_id = futures.pop(future)
                try:
                    result = future.result()
                except Exception as error:
                    result = {
                        "id": task_id,
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                    }
                results.append(result)
                print(
                    f"{name}: {len(results)}/{len(task_ids)} {result['id']} {result['status']}",
                    flush=True,
                )
    changed = implementation_digest() != implementation
    if changed:
        for result in results:
            record = read_record(dataset, result["id"])
            if record.get("verification"):
                record["verification"].update(
                    valid=False, error="Authoring implementation changed during batch"
                )
                record["status"] = "needs_reverification"
                _persist(dataset, record)
                result["status"] = "needs_reverification"
    report = {
        "implementation_sha256": implementation,
        "implementation_changed": changed,
        "expected_tasks": 800,
        "selected": len(task_ids),
        "results": sorted(results, key=lambda result: result["id"]),
    }
    write_json(Path(dataset) / f"{name}-report.json", report)
    return report


def verify_dataset(dataset: Path, task_ids: list[str], workers=4):
    """Refresh every selected candidate's content-bound, key-free verification."""
    manifest, tasks = source_tasks(dataset)
    _check_open(manifest)

    def worker(task_id):
        record = read_record(dataset, task_id)
        if not record.get("draft"):
            return {"id": task_id, "status": "missing"}
        draft = SeriesDraft.model_validate(record["draft"])
        try:
            result, execution = verify_draft(
                tasks[task_id], Path(manifest["source_root"]), draft
            )
            result["implementation_sha256"] = implementation_digest()
            record.update(
                verification=result,
                status="verified" if len(draft.stages) == 2 else "partial",
            )
            write_json(
                Path(dataset) / "journals" / f"{digest(task_id)}.json",
                execution.records,
            )
        except Exception as error:
            candidate_error = isinstance(error, CandidateValidationError)
            record.update(
                verification={
                    "valid": False,
                    "content_sha256": digest(draft),
                    "error": f"{type(error).__name__}: {error}",
                },
                status="needs_revision" if candidate_error else "verification_error",
            )
        _persist(dataset, record)
        return {
            "id": task_id,
            "status": record["status"],
            "verification": record["verification"],
        }

    report = _batch(dataset, task_ids, worker, workers, "verification")
    write_jsonl(Path(dataset) / "verification.jsonl", report["results"])
    return report


def review_dataset(
    dataset: Path,
    config,
    task_ids: list[str],
    *,
    concurrency: int | None = None,
    request_policy: RequestPolicy | None = None,
    write_sheets: bool = True,
):
    """Independently review checked full series and export their human review sheets."""
    from tau2.task_authoring.gaia2_series.generation import (
        ProviderQuotaExhausted,
        ProviderUnavailable,
        cancel_requested,
        request_policy_override,
        resolve_profile,
        review_series,
        sanitize,
    )

    manifest, tasks = source_tasks(dataset)
    _check_open(manifest)
    identity = _configuration(dataset, config)
    stop = threading.Event()
    policy = request_policy or RequestPolicy()
    workers = config.concurrency if concurrency is None else concurrency
    if workers < 1:
        raise ValueError("concurrency must be positive")

    def worker(task_id):
        cancel_requested.set(stop)
        request_policy_override.set(policy)
        record = read_record(dataset, task_id)
        if not record.get("draft"):
            return {"id": task_id, "status": "missing"}
        draft = SeriesDraft.model_validate(record["draft"])
        if len(draft.stages) != 2:
            return {"id": task_id, "status": "partial"}
        fingerprint = digest(draft)
        old = record.get("review", {})
        if (
            old.get("content_sha256") == fingerprint
            and old.get("config_identity") == identity
            and old.get("implementation_sha256") == implementation_digest()
        ):
            return {
                "id": task_id,
                "status": "reviewed" if old["accepted"] else "rejected",
            }
        if stop.is_set():
            return {"id": task_id, "status": "not_attempted_provider_unavailable"}
        try:
            verification, execution = verify_draft(
                tasks[task_id], Path(manifest["source_root"]), draft
            )
        except Exception as error:
            _, _, secrets = resolve_profile(config, config.reviewer)
            record.update(
                status=(
                    "needs_revision"
                    if isinstance(error, CandidateValidationError)
                    else "verification_error"
                ),
                last_error=sanitize(f"{type(error).__name__}: {error}", secrets),
            )
            _persist(dataset, record)
            return {"id": task_id, "status": record["status"]}
        record["verification"] = {
            **verification,
            "implementation_sha256": implementation_digest(),
        }
        record.setdefault("calls", [])
        record["calls"] = PersistentCalls(record["calls"], dataset, record)
        try:
            context = context_packet(
                dataset, tasks[task_id], execution.states[2], config.context_characters
            )
            decision = review_series(
                config,
                tasks[task_id],
                Path(manifest["source_root"]),
                execution,
                draft,
                context,
                record["calls"],
            )
            record["review"] = {
                "content_sha256": fingerprint,
                "config_identity": identity,
                "implementation_sha256": implementation_digest(),
                "decision": decision.model_dump(),
                "accepted": decision.accepted,
            }
            if decision.material_warnings or not decision.accepted:
                record["anomaly_flags"] = sorted(
                    set(record.get("anomaly_flags", []))
                    | {
                        "model_review_warning"
                        if decision.material_warnings
                        else "model_review_rejected"
                    }
                )
            record["status"] = "reviewed" if decision.accepted else "needs_revision"
        except ProviderUnavailable as error:
            if policy.stop_batch_on_error or isinstance(error, ProviderQuotaExhausted):
                stop.set()
            record.update(status="provider_error", last_error=str(error))
        except Exception as error:
            _, _, secrets = resolve_profile(config, config.reviewer)
            record.update(
                status="review_error",
                last_error=sanitize(f"{type(error).__name__}: {error}", secrets),
            )
        finally:
            _persist(dataset, record)
        return {"id": task_id, "status": record["status"]}

    report = _batch(dataset, task_ids, worker, workers, "review")
    report.update(concurrency=workers, request_policy=policy.model_dump(mode="json"))
    write_json(Path(dataset) / "review-report.json", report)
    if write_sheets:
        review_sheet(dataset, task_ids)
    write_jsonl(
        Path(dataset) / "review.jsonl",
        (
            {
                "base_task_id": task_id,
                "review": read_record(dataset, task_id).get("review"),
                "human": read_record(dataset, task_id).get("human"),
            }
            for task_id in sorted(tasks)
        ),
    )
    return report


def review_sheet(dataset: Path, task_ids: list[str]):
    """Export readable tasks and unsigned decisions; never invent human approval."""
    manifest, tasks = source_tasks(dataset)
    lines = [
        "# Gaia2 continuation review",
        "",
        "Unsigned templates are not approvals.",
        "",
    ]
    requests = [
        "# Gaia2 pilot requests",
        "",
        f"Selected tasks: {len(task_ids)}. Human review is pending.",
        "",
    ]
    templates = []
    required = human_review_ids(dataset, manifest, tasks)
    groups = duplicate_groups(dataset, tasks)
    required.update(task_id for group in groups for task_id in group)
    write_json(
        Path(dataset) / "duplicates.json",
        {
            "potential_duplicate_groups": groups,
            "action": "Require human review; wording alone is not rejection",
        },
    )
    for task_id in task_ids:
        record = read_record(dataset, task_id)
        requests.extend(
            [
                f"## {task_id}",
                "",
                f"Status: {record.get('status', 'pending')}; "
                f"model accepted: {record.get('review', {}).get('accepted', False)}.",
                "",
            ]
        )
        if not record.get("draft"):
            requests.extend(["No complete draft is available.", ""])
            continue
        draft = SeriesDraft.model_validate(record["draft"])
        base = tasks[task_id]
        original_request = (
            base.user_scenario.instructions
            if isinstance(base.user_scenario.instructions, str)
            else base.user_scenario.instructions.reason_for_call
        )
        requests.extend(["### Original", "", original_request, ""])
        lines.extend(
            [
                f"## {task_id}",
                "",
                f"Content SHA256: `{digest(draft)}`",
                "",
                f"Status: {record.get('status', 'pending')}",
                "",
                f"Required human sample: {task_id in required}",
                "",
                "### Original",
                "",
                (
                    base.user_scenario.instructions
                    if isinstance(base.user_scenario.instructions, str)
                    else base.user_scenario.instructions.reason_for_call
                ),
                "",
            ]
        )
        for number, stage in enumerate(draft.stages, 1):
            requests.extend(
                [
                    f"### Continuation {number}",
                    "",
                    stage.request,
                    "",
                    f"Relation: {stage.relation}",
                    "",
                ]
            )
            for reference in stage.events:
                if reference.channel == "agent":
                    requests.extend(
                        [
                            "Reference reply:",
                            "",
                            str(reference.arguments.get("content", "")),
                            "",
                        ]
                    )
            lines.extend(
                [
                    f"### Continuation {number}",
                    "",
                    stage.request,
                    "",
                    stage.relation,
                    "",
                    "```json",
                    json.dumps(
                        stage.model_dump(mode="json"), ensure_ascii=False, indent=2
                    ),
                    "```",
                    "",
                ]
            )
        lines.extend(
            [
                "### Automated checks",
                "",
                "```json",
                json.dumps(
                    {
                        "verification": record.get("verification"),
                        "review": record.get("review"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
        templates.append(
            {
                "base_task_id": task_id,
                "content_sha256": digest(draft),
                "reviewer": "",
                "decision": "PENDING",
                "notes": "",
            }
        )
    (Path(dataset) / "review.md").write_text("\n".join(lines))
    (Path(dataset) / "requests.md").write_text("\n".join(requests))
    write_jsonl(Path(dataset) / "human-review-template.jsonl", templates)


def import_reviews(dataset: Path, decisions: Path):
    """Validate all signatures before writing any; reject stale hashes and duplicates."""
    manifest, tasks = source_tasks(dataset)
    _check_open(manifest)
    values = [
        HumanDecision.model_validate_json(line)
        for line in Path(decisions).read_text().splitlines()
        if line.strip()
    ]
    if len({value.base_task_id for value in values}) != len(values):
        raise ValueError("Duplicate human review decisions")
    records = []
    for value in values:
        if value.base_task_id not in tasks:
            raise ValueError("Human review refers to an unknown source task")
        record = read_record(dataset, value.base_task_id)
        if not record.get("draft") or digest(record["draft"]) != value.content_sha256:
            raise ValueError(
                f"Human review content hash is stale: {value.base_task_id}"
            )
        record["human"] = value.model_dump()
        records.append(record)
    for record in records:
        _persist(dataset, record)
    return {"imported": len(records)}


def acceptance_errors(
    record: dict, *, human_required: bool, implementation: str
) -> list[str]:
    """Apply export gates to exact content, not mutable status labels."""
    if not record.get("draft"):
        return ["missing_draft"]
    draft = SeriesDraft.model_validate(record["draft"])
    fingerprint = digest(draft)
    errors = []
    if len(draft.stages) != 2:
        errors.append("requires_two_continuations")
    verification = record.get("verification", {})
    if (
        not verification.get("valid")
        or not verification.get("replay_verified")
        or verification.get("content_sha256") != fingerprint
        or verification.get("implementation_sha256") != implementation
    ):
        errors.append("missing_or_stale_verification")
    review = record.get("review", {})
    if (
        review.get("content_sha256") != fingerprint
        or review.get("implementation_sha256") != implementation
        or not ReviewDecision.model_validate(
            review.get(
                "decision",
                {
                    key: False
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                },
            )
        ).accepted
    ):
        errors.append("missing_or_stale_model_review")
    human = record.get("human", {})
    if human_required or human:
        if (
            human.get("decision") != "accepted"
            or human.get("content_sha256") != fingerprint
            or not human.get("reviewer")
        ):
            errors.append("missing_or_stale_human_review")
    return errors


def duplicate_groups(dataset: Path, tasks: dict) -> list[list[str]]:
    """Flag repeated goals on the same material, retaining targets and expected effects."""
    groups = {}
    for task_id, base in tasks.items():
        record = read_record(dataset, task_id)
        if not record.get("draft"):
            continue
        draft = SeriesDraft.model_validate(record["draft"])
        if len(draft.stages) != 2:
            continue
        content = []
        for stage in draft.stages:
            content.append(
                {
                    "request": " ".join(stage.request.casefold().split()),
                    "effects": [
                        (
                            event.tool,
                            REFERENCE.sub(
                                "{{prior}}", json.dumps(event.arguments, sort_keys=True)
                            ),
                        )
                        for event in stage.events
                    ],
                    "expected": [
                        (check.operation, check.expected) for check in stage.checks
                    ],
                }
            )
        key = digest({"snapshot": base.scenario.snapshot, "goals": content})
        groups.setdefault(key, []).append(task_id)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)


def similar_groups(dataset: Path, tasks: dict) -> list[list[str]]:
    """Flag near-identical requests using the same snapshot and operation pattern."""
    buckets = {}
    for task_id, task in tasks.items():
        record = read_record(dataset, task_id)
        stages = (record.get("draft") or {}).get("stages", [])
        if len(stages) != 2:
            continue
        operations = tuple(
            event["tool"] for stage in stages for event in stage["events"]
        )
        text = " ".join(stage["request"].casefold() for stage in stages)
        buckets.setdefault((task.scenario.snapshot, operations), []).append(
            (task_id, text)
        )
    pairs = []
    for group in buckets.values():
        for index, (task_id, text) in enumerate(group):
            for other_id, other in group[index + 1 :]:
                if SequenceMatcher(None, text, other).ratio() >= 0.9:
                    pairs.append(sorted([task_id, other_id]))
    return sorted(pairs)


def export_dataset(dataset: Path, *, allow_partial=False):
    """Freeze only complete, reviewed populations; partial exports stay explicitly draft."""
    manifest, tasks = source_tasks(dataset)
    _check_open(manifest)
    required = human_review_ids(dataset, manifest, tasks)
    groups = duplicate_groups(dataset, tasks)
    required.update(task_id for group in groups for task_id in group)
    write_json(
        Path(dataset) / "duplicates.json", {"potential_duplicate_groups": groups}
    )
    implementation = implementation_digest()
    accepted, failures, sidecars = [], [], []
    two_stage = []
    for task_id in sorted(tasks):
        record = read_record(dataset, task_id)
        try:
            errors = acceptance_errors(
                record,
                human_required=task_id in required,
                implementation=implementation,
            )
        except Exception as error:
            errors = [f"invalid_record: {error}"]
        if errors:
            failures.append({"base_task_id": task_id, "errors": errors})
            continue
        draft = SeriesDraft.model_validate(record["draft"])
        # Export independently re-executes current bytes; saved success flags are insufficient.
        try:
            verify_draft(tasks[task_id], Path(manifest["source_root"]), draft)
        except Exception as error:
            failures.append(
                {
                    "base_task_id": task_id,
                    "errors": [
                        f"reference_recheck_failed: {type(error).__name__}: {error}"
                    ],
                }
            )
            continue
        task, stages = compile_series(tasks[task_id], draft)
        prefix = draft.model_copy(deep=True)
        prefix.stages = prefix.stages[:1]
        short, _ = compile_series(tasks[task_id], prefix)
        accepted.append(task.model_dump(mode="json"))
        two_stage.append(short.model_dump(mode="json"))
        sidecars.append(
            {
                "base_task_id": task_id,
                "series_id": task.id,
                "base_task_sha256": digest(tasks[task_id]),
                "content_sha256": digest(draft),
                "stages": stages,
                "verification": record["verification"],
                "review": record["review"],
                "human": record.get("human"),
            }
        )
    report = {
        "expected_tasks": 800,
        "accepted": len(accepted),
        "added_requests": 2 * len(accepted),
        "complete": not failures and len(accepted) == 800,
        "failures": failures,
        "execution_ready": False,
    }
    write_json(Path(dataset) / "coverage.json", report)
    if not report["complete"] and not allow_partial:
        raise ValueError(
            f"Full export requires 800 accepted series; got {len(accepted)}. See coverage.json"
        )
    output = Path(dataset) / ("release" if report["complete"] else "draft-export")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "tasks.json", accepted)
    write_json(output / "tasks-2.json", two_stage)
    write_jsonl(output / "stages.jsonl", sidecars)
    splits = {
        category: [
            task["id"] for task in accepted if task["scenario"]["category"] == category
        ]
        for category in ["search", "execution", "ambiguity", "adaptability", "time"]
    }
    splits["base"] = [task["id"] for task in accepted]
    write_json(output / "split_tasks.json", splits)
    release = {
        "version": VERSION,
        **report,
        "source_files": manifest["source_files"],
        "runtime_sha256": manifest["runtime_sha256"],
        "authoring_sha256": implementation,
        "artifact_hashes": {
            p.name: file_digest(p)
            for p in sorted(output.iterdir())
            if p.is_file() and p.name != "manifest.json"
        },
    }
    write_json(output / "manifest.json", release)
    if report["complete"]:
        manifest.update(status="frozen", release_sha256=digest(release))
        write_json(Path(dataset) / "manifest.json", manifest)
    return report

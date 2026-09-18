"""One-case-at-a-time launcher with fail-fast measurement gates and safe stop.

No service requests are made without --services-restored. This is an explicit
operator assertion, not a claim that a billing console was automatically checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from adapters.gaia2.evaluation_campaign import digest, verify_freeze, write_json

REGRESSION_PREFIXES = (
    "0680_",
    "0059_",
    "0797_",
    "0215_",
    "0732_",
    "0777_",
    "0084_",
    "0651_",
    "0036_",
    "0436_",
    "0597_",
    "0010_",
    "0498_",
)
SMOKE10_PREFIXES = (
    "0084_",
    "0036_",
    "0651_",
    "0732_",
    "0597_",
    "0059_",
    "0436_",
    "0680_",
    "0797_",
    "0215_",
)
VALID_OUTCOMES = {"completed", "protocol_failure", "execution_timeout"}


def stops_batch(result):
    if result.get("protocol") in {
        "gaia2-qwen-audio/5",
        "gaia2-qwen-audio/6",
        "gaia2-qwen-audio/7",
        "gaia2-qwen-audio/8",
    }:
        from adapters.gaia2.qwen_audio_template.environment.runner.evidence import (
            validate_grade,
        )

        try:
            validate_grade(result)
        except ValueError:
            return True
        return False
    return (
        result.get("measurement_valid") is False
        or result.get("status") not in VALID_OUTCOMES
    )


def selection(manifest, stage):
    rows = manifest["seed_cases"]
    if stage == "remaining":
        used = {r["source_id"] for r in rows}
        rows = [r for r in manifest["eligible"] if r["source_id"] not in used]
    if stage in {"regression", "smoke10"}:
        selected = []
        for prefix in SMOKE10_PREFIXES if stage == "smoke10" else REGRESSION_PREFIXES:
            matches = [r for r in rows if r["source_id"].startswith(prefix)]
            if len(matches) != 1:
                raise ValueError(f"Missing unique diagnostic case {prefix}")
            selected.extend(matches)
        return [(r, "audio") for r in selected]
    tracks = ["system_text", "backend_text"] if stage == "controls" else ["audio"]
    return [(r, t) for t in tracks for r in rows]


def window_results(job):
    by_id = {}
    for path in job.glob("*/agent/gaia2_qwen_audio_result.json"):
        payload = json.loads(path.read_text())
        identity = payload.get("audio", {}).get("source_id")
        if identity in by_id:
            raise ValueError("Duplicate result for one scheduled case")
        framework_path = path.parent.parent / "result.json"
        framework = (
            json.loads(framework_path.read_text()) if framework_path.exists() else {}
        )
        if framework.get("exception_info") and (
            payload.get("measurement_valid") is True
            or payload.get("status") == "completed_unscored"
        ):
            payload = {
                **payload,
                "status": "infrastructure_error",
                "success": None,
                "measurement_valid": False,
                "failure_cause": "harbor_verifier_or_lifecycle",
            }
        by_id[identity] = payload
    return by_id


def record_stopped_framework(job):
    path = job / "result.json"
    if not path.exists():
        return
    original = path.read_bytes()
    saved = job / "framework-result-before-stop.json"
    if not saved.exists():
        saved.write_bytes(original)
    data = json.loads(original)
    stats = data.get("stats", {})
    stats["n_cancelled_trials"] = (
        stats.get("n_cancelled_trials", 0)
        + stats.get("n_running_trials", 0)
        + stats.get("n_pending_trials", 0)
    )
    stats.update(n_running_trials=0, n_pending_trials=0)
    data["stats"] = stats
    data["finished_at"] = datetime.now(UTC).isoformat()
    data["stop_reason"] = "user_requested"
    write_json(path, data)


def archive_stopped_job(job, output):
    """Operate only on containers whose exact Compose project matches this job."""
    snapshots = []
    for trial in job.iterdir() if job.exists() else []:
        if not trial.is_dir() or not (trial / "config.json").exists():
            continue
        project = trial.name.lower() + "__env"
        found = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        containers = found.stdout.splitlines()
        for name in containers:
            if name.endswith("are-runner-1"):
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        name,
                        "python",
                        "-c",
                        "from pathlib import Path; Path('/evaluation/stop-requested').touch()",
                    ],
                    check=False,
                    capture_output=True,
                )
        # Runner writes an explicit aborted result; deadline also bounds a stuck provider.
        deadline = time.monotonic() + 30
        runner = next((n for n in containers if n.endswith("are-runner-1")), None)
        while runner and time.monotonic() < deadline:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    runner,
                    "test",
                    "-f",
                    "/evaluation/results/result.json",
                ],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        destination = output / trial.name
        destination.mkdir(parents=True, exist_ok=True)
        for name in containers:
            source = (
                "/evaluation/results"
                if name.endswith("are-runner-1")
                else "/logs/agent/qwen-gateway.log"
            )
            target = destination / (
                "results" if name.endswith("are-runner-1") else "qwen-gateway.log"
            )
            copied = subprocess.run(
                ["docker", "cp", f"{name}:{source}", str(target)],
                capture_output=True,
                check=False,
            )
            snapshots.append({"container": name, "copied": copied.returncode == 0})
            subprocess.run(["docker", "kill", name], capture_output=True, check=False)
    return snapshots


def value_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def retry_evidence(campaign, entry, manifest):
    """Verify one separate ASR-service retry without replacing the failed trial."""
    result = entry["result"]
    if (
        result.get("status") != "infrastructure_error"
        or result.get("failure_cause") != "provider_asr_failure"
    ):
        raise ValueError(
            "Only a diagnosed ASR service failure can use this resume path"
        )
    source_id = entry["source_id"]
    config_path = campaign / "retries" / f"{source_id}-attempt-1.yaml"
    provenance = json.loads(config_path.with_suffix(".provenance.json").read_text())
    if (
        provenance.get("manifest_sha256") != digest(campaign / "manifest.json")
        or provenance.get("first_job") != entry["job"]
        or provenance.get("first_result_sha256") != value_digest(result)
        or provenance.get("config_sha256") != digest(config_path)
        or provenance.get("attempt") != 1
        or provenance.get("not_part_of_first_attempt_score") is not True
    ):
        raise ValueError("Infrastructure retry provenance changed or is incomplete")
    original = window_results(Path(entry["job"]))
    if source_id not in original or value_digest(original[source_id]) != value_digest(
        result
    ):
        raise ValueError("Original failure evidence differs from the primary ledger")
    row = next(r for r in manifest["eligible"] if r["source_id"] == source_id)
    expected = yaml.safe_load((campaign / f"{entry['track']}.yaml").read_text())
    expected.update(
        job_name=f"infra-retry-1-{row['task_name']}",
        jobs_dir=str(config_path.parent.resolve()),
        n_attempts=1,
        n_concurrent_trials=1,
        retry={"max_retries": 0},
    )
    expected["datasets"][0]["task_names"] = [row["task_name"]]
    config = yaml.safe_load(config_path.read_text())
    if config != expected:
        raise ValueError(
            "Retry must use the identical agent, data and model configuration"
        )
    job = Path(config["jobs_dir"]) / config["job_name"]
    retried = window_results(job)
    if set(retried) != {source_id}:
        raise ValueError("Need exactly one completed retry result")
    retry = retried[source_id]
    if (
        stops_batch(retry)
        or retry.get("measurement_valid") is not True
        or retry.get("protocol") != manifest["protocol"]
        or retry.get("source") != manifest["source"]
        or not result.get("models")
        or retry.get("models") != result["models"]
        or retry.get("track") != entry["track"]
        or retry.get("audio", {}).get("sha256") != row["sha256"]
    ):
        raise ValueError(
            "Retry has not verified service recovery on the same frozen system"
        )
    return {
        "source_id": source_id,
        "track": entry["track"],
        "manifest_sha256": digest(campaign / "manifest.json"),
        "first_result_sha256": value_digest(result),
        "config_sha256": digest(config_path),
        "retry_job": str(job),
        "retry_result_sha256": value_digest(retry),
        "preserve_first_attempt": True,
        "replace_task_score": False,
    }


def reviewed_infrastructure(campaign, entry, manifest):
    path = campaign / "retries" / f"{entry['source_id']}-attempt-1.review.json"
    if not path.exists():
        return False
    review = json.loads(path.read_text())
    evidence = retry_evidence(campaign, entry, manifest)
    if review.get("accepted") is not True or any(
        review.get(k) != v for k, v in evidence.items()
    ):
        raise ValueError("Infrastructure review or its evidence changed")
    return True


def accept_infrastructure_retry(campaign, source_id):
    manifest = verify_freeze(campaign)
    ledger = json.loads((campaign / "execution-ledger.json").read_text())
    if ledger["manifest_sha256"] != digest(campaign / "manifest.json"):
        raise ValueError("Ledger belongs to another freeze")
    entries = [
        r
        for r in ledger["results"]
        if r["source_id"] == source_id
        and r["result"].get("status") == "infrastructure_error"
    ]
    if len(entries) != 1:
        raise ValueError("Exactly one recorded infrastructure failure is required")
    evidence = retry_evidence(campaign, entries[0], manifest)
    path = campaign / "retries" / f"{source_id}-attempt-1.review.json"
    if path.exists():
        reviewed_infrastructure(campaign, entries[0], manifest)
        return path
    write_json(
        path,
        {
            **evidence,
            "accepted": True,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "scope": "Resume diagnostic admission only; keep original failure and scale gates unchanged",
        },
    )
    return path


def run(campaign, stage, services_restored=False, concurrency=1, max_attempts=None):
    if not services_restored:
        raise ValueError(
            "Services remain unverified. Restore DashScope, then explicitly pass --services-restored; no API calls were made."
        )
    from adapters.gaia2.evaluation_campaign import audit_digest

    manifest = verify_freeze(campaign)
    windowed = manifest.get("protocol") in {
        "gaia2-qwen-audio/4",
        "gaia2-qwen-audio/5",
        "gaia2-qwen-audio/6",
        "gaia2-qwen-audio/7",
        "gaia2-qwen-audio/8",
    }
    if windowed:
        if stage != "audio" or concurrency != 1:
            raise ValueError("V4 initially requires stage audio and concurrency 1")
        proof_path = campaign / "observation-validation.json"
        proof = json.loads(proof_path.read_text())
        if proof.get("passed") is not True or proof.get(
            "adapter_files_sha256"
        ) != value_digest(manifest["adapter_files"]):
            raise ValueError("Missing matching deterministic observation validation")
        for evidence in proof.get("artifacts", []):
            if digest(Path(evidence["path"])) != evidence["sha256"]:
                raise ValueError("Observation validation evidence changed")
        if not proof.get("artifacts"):
            raise ValueError("Observation validation requires evidence artifacts")
    elif manifest.get("protocol") == "gaia2-qwen-audio/3":
        from adapters.gaia2.qwen_audio_template.environment.runner.boundary import (
            PROMPT,
            PROMPT_VERSION,
        )

        calibration_path = campaign / "boundary-calibration/summary.json"
        if not calibration_path.exists():
            raise ValueError(
                "Automatic boundary stability evidence is required before live trials"
            )
        calibration = json.loads(calibration_path.read_text())
        if (
            calibration.get("passed") is not True
            or calibration.get("examples", 0) < 60
            or calibration.get("validation_mode") != "automatic_stability"
            or calibration.get("resolved_examples") != calibration.get("examples")
            or calibration.get("prompt_version") != PROMPT_VERSION
            or calibration.get("prompt_sha256")
            != hashlib.sha256(PROMPT.encode()).hexdigest()
            or calibration.get("model") != manifest["profile"]["judge_model"]
            or calibration.get("effort") != manifest["profile"]["judge_effort"]
        ):
            raise ValueError(
                "Boundary calibration does not match the frozen assessor/profile"
            )
        from adapters.gaia2.boundary_review import verify_calibration

        verify_calibration(campaign, manifest["profile"])
    else:
        raise ValueError("Unsupported campaign protocol")
    limit = manifest.get("attempt_limit")
    if max_attempts is not None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts <= 0
        ):
            raise ValueError("max_attempts must be a positive integer")
        limit = min(limit, max_attempts) if limit else max_attempts
    if stage in {"regression", "smoke10", "audio", "controls"} and concurrency != 1:
        raise ValueError("Initial regression and pilot tracks use concurrency 1")
    if stage == "controls" and not (campaign / "audio-pilot-gate.json").exists():
        raise ValueError("Complete and audit the audio pilot before text controls")
    if (
        stage == "controls"
        and json.loads((campaign / "audio-pilot-gate.json").read_text()).get("passed")
        is not True
    ):
        raise ValueError("Audio pilot gate has not passed")
    if stage == "remaining":
        gate = json.loads((campaign / "gate.json").read_text())
        expansion = json.loads((campaign / "expansion.json").read_text())
        from adapters.gaia2.evaluation_campaign import freeze_files

        if (
            gate.get("passed") is not True
            or gate.get("manifest_sha256") != digest(campaign / "manifest.json")
            or gate.get("audit_sha256") != audit_digest(campaign)
            or gate.get("evidence_sha256") != digest(Path(gate["evidence_path"]))
            or expansion.get("manifest_sha256") != digest(campaign / "manifest.json")
            or freeze_files(campaign / "remaining-dataset") != expansion.get("files")
        ):
            raise ValueError("Expansion gate or frozen artifacts changed")
    ledger_path = campaign / (
        f"load-{concurrency}-ledger.json"
        if stage == "load"
        else "execution-ledger.json"
    )
    fingerprint = digest(campaign / "manifest.json")
    ledger: dict[str, Any] = (
        json.loads(ledger_path.read_text())
        if ledger_path.exists()
        else {
            "manifest_sha256": fingerprint,
            "results": [],
            "interrupted": [],
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    if ledger["manifest_sha256"] != fingerprint:
        raise ValueError("Ledger belongs to another freeze")
    if any(
        stops_batch(r["result"]) and not reviewed_infrastructure(campaign, r, manifest)
        for r in ledger["results"]
    ):
        raise ValueError(
            "A prior measurement/service failure requires investigation; do not silently rerun"
        )
    if windowed and any(
        r.get("audit", {}).get("passed") is not True for r in ledger["results"]
    ):
        raise ValueError("Prior v4 audit is unresolved; preserve original results")
    if windowed:
        from adapters.gaia2.window_audit import verify_saved_audit

        for entry in ledger["results"]:
            verify_saved_audit(campaign, entry)
    requested = selection(manifest, stage)
    if windowed and any(
        r.get("protocol_support", {}).get("supported") is not True for r, _ in requested
    ):
        raise ValueError(
            "Selection includes unsupported_protocol scenarios; no replacements are allowed"
        )
    if stage == "load":
        requested = [
            next((r, t) for r, t in requested if r["config"] == config)
            for config in ["execution", "search", "adaptability", "time", "ambiguity"]
        ]
    if ledger["interrupted"]:
        raise ValueError(
            "Preserve the interrupted run. An explicit recovery plan is required before resumption."
        )
    done = {(r["source_id"], r["track"]) for r in ledger["results"]}
    stop = threading.Event()

    def request_stop(signum, frame):
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    (campaign / "stop-requested").unlink(missing_ok=True)
    try:
        ledger.update(
            stage=stage,
            state="running",
            supervisor_pid=os.getpid(),
            requested_attempt_limit=limit,
            services_restored_asserted_at=datetime.now(UTC).isoformat(),
        )
        write_json(ledger_path, ledger)
        pending = [
            (row, track)
            for row, track in requested
            if (row["source_id"], track) not in done
        ]
        while pending:
            if (
                limit is not None
                and len(ledger["results"]) + len(ledger["interrupted"]) >= limit
            ):
                ledger["state"] = "attempt_limit_reached"
                break
            if (campaign / "stop-after-current").exists():
                ledger["state"] = "stopped_after_current"
                break
            if stop.is_set() or (campaign / "stop-requested").exists():
                ledger["state"] = "stopped_by_user"
                break
            # A window admits at most concurrency trials. No next window is
            # launched until every result in this window passes measurement gates.
            track = pending[0][1]
            window_size = (
                concurrency
                if limit is None
                else min(
                    concurrency,
                    limit - len(ledger["results"]) - len(ledger["interrupted"]),
                )
            )
            window = [pair for pair in pending if pair[1] == track][:window_size]
            config = yaml.safe_load(
                (
                    campaign
                    / (
                        "audio-remaining.yaml"
                        if stage == "remaining"
                        else f"{track}.yaml"
                    )
                ).read_text()
            )
            config.update(
                job_name=f"{stage}-{track}-wave-{len(ledger['results']):04d}",
                n_concurrent_trials=concurrency,
            )
            if stage == "load":
                config["jobs_dir"] = str(
                    (campaign / f"load-{concurrency}-jobs").resolve()
                )
            config["datasets"][0]["task_names"] = [
                row["task_name"] for row, _ in window
            ]
            launch = campaign / "launches" / f"{config['job_name']}-{concurrency}.yaml"
            launch.parent.mkdir(parents=True, exist_ok=True)
            launch.write_text(yaml.safe_dump(config, sort_keys=False))
            job = Path(config["jobs_dir"]) / config["job_name"]
            if job.exists():
                raise ValueError(
                    f"Existing unrecorded job requires reconciliation: {job}"
                )
            ledger["active"] = {
                "cases": [r["source_id"] for r, _ in window],
                "track": track,
                "job": str(job),
            }
            write_json(ledger_path, ledger)
            with (campaign / "run.log").open("ab") as log:
                child = subprocess.Popen(
                    [sys.executable, "-m", "harbor.cli.main", "run", "-c", str(launch)],
                    env={
                        **os.environ,
                        "DOCKER_BUILDKIT": "0",
                        **(
                            {
                                "PATH": str(
                                    Path(manifest["metric_uv_executable"]).parent
                                )
                                + os.pathsep
                                + os.environ.get("PATH", "")
                            }
                            if manifest.get("protocol")
                            in {
                                "gaia2-qwen-audio/5",
                                "gaia2-qwen-audio/6",
                                "gaia2-qwen-audio/7",
                                "gaia2-qwen-audio/8",
                            }
                            else {}
                        ),
                    },
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                while child.poll() is None:
                    if stop.is_set() or (campaign / "stop-requested").exists():
                        os.killpg(child.pid, signal.SIGSTOP)
                        snapshots = archive_stopped_job(job, campaign / "interrupted")
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        completed = window_results(job)
                        record_stopped_framework(job)
                        for row, _ in window:
                            if (
                                row["source_id"] in completed
                                and completed[row["source_id"]].get("status")
                                != "user_aborted"
                            ):
                                ledger["results"].append(
                                    {
                                        "source_id": row["source_id"],
                                        "track": track,
                                        "job": str(job),
                                        "result": completed[row["source_id"]],
                                    }
                                )
                                done.add((row["source_id"], track))
                                continue
                            ledger["interrupted"].append(
                                {
                                    "source_id": row["source_id"],
                                    "track": track,
                                    "job": str(job),
                                    "status": "user_aborted",
                                    "snapshots": snapshots,
                                }
                            )
                        ledger["state"] = "stopped_by_user"
                        stop.set()
                        break
                    time.sleep(0.25)
            if stop.is_set():
                break
            by_id = window_results(job)
            failed = False
            for row, _ in window:
                result = by_id.get(
                    row["source_id"],
                    {
                        "status": "infrastructure_error",
                        "measurement_valid": False,
                        "failure_cause": "missing_trial_evidence",
                        "success": None,
                    },
                )
                ledger["results"].append(
                    {
                        "source_id": row["source_id"],
                        "track": track,
                        "job": str(job),
                        "result": result,
                    }
                )
                done.add((row["source_id"], track))
                failed |= stops_batch(result)
                if windowed:
                    from adapters.gaia2.window_audit import audit_trial

                    audit = audit_trial(campaign, row, job, result)
                    ledger["results"][-1]["audit"] = audit
                    failed |= audit["passed"] is not True
            ledger["active"] = None
            write_json(ledger_path, ledger)
            if windowed:
                from adapters.gaia2.window_report import write_report

                write_report(campaign, ledger, manifest)
            if failed:
                ledger["state"] = "blocked_on_measurement_or_service"
                break
            pending = [
                pair for pair in pending if (pair[0]["source_id"], pair[1]) not in done
            ]
        else:
            ledger["state"] = "stage_completed"
    except BaseException as error:
        ledger.update(state="launcher_error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        ledger["active"] = None
        ledger["updated_at"] = datetime.now(UTC).isoformat()
        ledger["planned"] = len(requested)
        ledger["completed_in_stage"] = sum(
            (r["source_id"], t) in done for r, t in requested
        )
        ledger["not_started_in_stage"] = (
            len(requested)
            - ledger["completed_in_stage"]
            - sum(
                (r["source_id"], r["track"])
                in {(q["source_id"], t) for q, t in requested}
                for r in ledger["interrupted"]
            )
        )
        write_json(ledger_path, ledger)
        if windowed:
            from adapters.gaia2.window_report import write_report

            write_report(campaign, ledger, manifest)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return ledger


def prepare_infrastructure_retry(campaign, source_id):
    manifest = verify_freeze(campaign)
    ledger = json.loads((campaign / "execution-ledger.json").read_text())
    if ledger["manifest_sha256"] != digest(campaign / "manifest.json"):
        raise ValueError("Retry must use the identical frozen source")
    entries = [
        r
        for r in ledger["results"]
        if r["source_id"] == source_id
        and r["result"].get("status") == "infrastructure_error"
    ]
    if len(entries) != 1:
        raise ValueError(
            "Exactly one recorded infrastructure failure is required; model/measurement failures are not retry candidates"
        )
    entry = entries[0]
    row = next(r for r in manifest["eligible"] if r["source_id"] == source_id)
    path = campaign / "retries" / f"{source_id}-attempt-1.yaml"
    if path.exists():
        raise ValueError("The single independent retry has already been prepared")
    config = yaml.safe_load((campaign / f"{entry['track']}.yaml").read_text())
    config.update(
        job_name=f"infra-retry-1-{row['task_name']}",
        jobs_dir=str(path.parent.resolve()),
        n_attempts=1,
        n_concurrent_trials=1,
        retry={"max_retries": 0},
    )
    config["datasets"][0]["task_names"] = [row["task_name"]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    write_json(
        path.with_suffix(".provenance.json"),
        {
            "source_id": source_id,
            "first_job": entry["job"],
            "manifest_sha256": ledger["manifest_sha256"],
            "attempt": 1,
            "not_part_of_first_attempt_score": True,
            "config_sha256": digest(path),
            "first_result_sha256": value_digest(entry["result"]),
        },
    )
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument(
        "--stage",
        choices=["regression", "smoke10", "audio", "controls", "load", "remaining"],
        default="regression",
    )
    parser.add_argument("--concurrency", choices=[1, 2, 4], type=int, default=1)
    parser.add_argument("--services-restored", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--stop-after-current", action="store_true")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--prepare-infra-retry")
    parser.add_argument("--accept-infra-retry")
    args = parser.parse_args()
    if args.accept_infra_retry:
        print(accept_infrastructure_retry(args.campaign, args.accept_infra_retry))
        return
    if args.prepare_infra_retry:
        print(prepare_infrastructure_retry(args.campaign, args.prepare_infra_retry))
        return
    if args.stop_after_current:
        (args.campaign / "stop-after-current").touch()
        print("Will stop after the active trial and its audit; no next trial admitted.")
        return
    if args.stop:
        (args.campaign / "stop-requested").touch()
        print(
            "Stop requested; scheduler will archive the active trial and update the ledger."
        )
        return
    result = run(
        args.campaign,
        args.stage,
        args.services_restored,
        args.concurrency,
        args.max_attempts,
    )
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in [
                    "state",
                    "planned",
                    "completed_in_stage",
                    "not_started_in_stage",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()

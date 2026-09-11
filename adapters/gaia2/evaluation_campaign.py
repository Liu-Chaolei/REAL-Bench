"""Freeze selection, prepare three-track jobs, and gate large audio campaigns.

uv run python -m adapters.gaia2.evaluation_campaign prepare --output ...
uv run python -m adapters.gaia2.evaluation_campaign audit --campaign ... --jobs ...
uv run python -m adapters.gaia2.evaluation_campaign gate --campaign ... --evidence ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from adapters.gaia2.adapter import Gaia2Adapter
from adapters.gaia2.audio import find_asset
from adapters.gaia2.qwen_audio_tasks import generate_audio_task, stage_qwen
from adapters.gaia2.qwen_audio_template.environment.runner.protocol import VERSION

CONFIGS = ("execution", "search", "adaptability", "time", "ambiguity")
TRACKS = ("audio", "system_text", "backend_text")
DEBUG_IDS = {
    "0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm",
    "0324_saa8qnpcx8r5eq5x4izam1tzn4c8z89m",
    "0520_vs2mfw4rol4q163x0lzp2f907btnnsmk",
    "0645_gbj8otuliajhbbuncqqie6i64yv36egn",
    "0770_wr11p4jja84co11mmhp640n3cvz64etj",
}


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def select(rows, seed=20260909):
    rng = random.Random(seed)
    selected = []
    for config in CONFIGS:
        candidates = sorted(
            [
                r
                for r in rows
                if r["config"] == config and r["source_id"] not in DEBUG_IDS
            ],
            key=lambda r: (r["duration_sec"], r["source_id"]),
        )
        if len(candidates) < 10:
            raise ValueError(f"Need at least 10 non-debug cases for {config}")
        strata = defaultdict(list)
        for index, row in enumerate(candidates):
            row["stratum"] = [
                min(2, index * 3 // len(candidates)),
                min(2, row["formal_replies"]),
                min(2, row["actions"] // 5),
            ]
            strata[tuple(row["stratum"])].append(row)
        # Round robin across rare strata prevents duration alone dominating.
        groups = list(strata.values())
        rng.shuffle(groups)
        groups.sort(key=len)
        for group in groups:
            rng.shuffle(group)
        chosen = []
        while len(chosen) < 10:
            for group in groups:
                if group and len(chosen) < 10:
                    chosen.append(group.pop())
        selected.extend(chosen)
    return selected


def freeze_files(root):
    return {
        str(p.relative_to(root)): digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and (
            p.suffix in {".py", ".mjs", ".md", ".yaml", ".toml", ".json"}
            or p.name.startswith("Dockerfile")
        )
        and "__pycache__" not in p.parts
    }


def profile_metadata(path):
    from dotenv import dotenv_values

    values = dotenv_values(path)
    config = json.loads(values.get("OPENCODE_CONFIG_CONTENT") or "{}")
    return {
        "speech_model": values.get("QWEN_AUDIO_REALTIME_MODEL"),
        "speech_provider": values.get("QWEN_AUDIO_REALTIME_PROVIDER") or "dashscope",
        "backend_model": values.get("QWEN_AUDIO_AGENT_BACKEND_MODEL"),
        "backend_effort": values.get("GAIA2_BACKEND_REASONING_EFFORT"),
        "backend_endpoint": config.get("provider", {})
        .get("openai", {})
        .get("options", {})
        .get("baseURL"),
        "title_model": config.get("small_model"),
        "judge_model": values.get("GAIA2_JUDGE_MODEL"),
        "judge_effort": values.get("GAIA2_JUDGE_REASONING_EFFORT"),
        "judge_api": values.get("GAIA2_JUDGE_API"),
        "judge_endpoint": values.get("GAIA2_JUDGE_ENDPOINT"),
    }


def prepare(args):
    import pyarrow.parquet as pq

    output = args.output.resolve()
    if (output / "manifest.json").exists():
        raise ValueError("Campaign is already frozen; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    rows, tasks, excluded = [], {}, []
    for config in CONFIGS:
        for shard in sorted((args.dataset / config).glob("validation-*.parquet")):
            for record in pq.read_table(shard).to_pylist():
                task = Gaia2Adapter.metadata_from_record(
                    record=record, config=config, split="validation"
                )
                try:
                    asset = find_asset(task, audio_dir=args.audio, manifest={})
                except (ValueError, OSError) as error:
                    excluded.append(
                        {
                            "config": config,
                            "source_id": task.source_id,
                            "reason": str(error),
                        }
                    )
                    continue
                events = json.loads(task.scenario_json)["events"]
                formal = sum(
                    e.get("action", {}).get("function") == "send_message_to_user"
                    for e in events
                )
                actions = sum(
                    e.get("event_type") == "AGENT"
                    and e.get("action", {}).get("app") != "AgentUserInterface"
                    for e in events
                )
                row = {
                    **asset.to_dict(),
                    "scenario_sha256": hashlib.sha256(
                        task.scenario_json.encode()
                    ).hexdigest(),
                    "formal_replies": formal,
                    "actions": actions,
                    "task_name": task.local_task_id,
                }
                rows.append(row)
                tasks[task.source_id] = (task, asset)
    from adapters.gaia2.qwen_audio_template.environment.runner.observation import (
        POLICY,
        eligibility,
    )
    from adapters.gaia2.qwen_audio_template.environment.runner.observation import (
        VERSION as WINDOW_VERSION,
    )

    windowed = getattr(args, "protocol", VERSION) == WINDOW_VERSION
    if windowed:
        for row in rows:
            row["protocol_support"] = eligibility(
                json.loads(tasks[row["source_id"]][0].scenario_json)
            )
    selected = select(rows)
    if getattr(args, "reuse_selection", None):
        prior = json.loads(args.reuse_selection.read_text())
        indexed = {row["source_id"]: row for row in rows}
        selected = []
        for old in prior["seed_cases"]:
            row = indexed.get(old["source_id"])
            if (
                row is None
                or row["sha256"] != old["sha256"]
                or row["scenario_sha256"] != old["scenario_sha256"]
            ):
                raise ValueError("Original selection or assets changed")
            selected.append(row)
    original_selection = [row["source_id"] for row in selected]
    selection_limit = getattr(args, "selection_limit", None)
    if selection_limit is not None:
        if selection_limit <= 0 or selection_limit > len(selected):
            raise ValueError("Selection limit must be within the original selection")
        selected = selected[:selection_limit]
    source = stage_qwen(args.qwen, output / "dataset")
    adapter = Gaia2Adapter(output / "dataset")
    for row in selected:
        task, asset = tasks[row["source_id"]]
        generate_audio_task(adapter, task, asset, source)
    manifest = {
        "protocol": WINDOW_VERSION if windowed else VERSION,
        "original_selection_ids": original_selection,
        "attempt_limit": len(selected) if windowed else None,
        "observation_policy": POLICY if windowed else None,
        "seed": 20260909,
        "label": "GAIA2 voice variant; automatic validation, not human calibrated; not original GAIA2 leaderboard parity",
        "review_policy": "automatic",
        "clock_policy": "real elapsed; setup and grading pauses excluded",
        "audio_policy": "preserve interference and background speech; no denoising",
        "models": {
            "speech": "qwen-audio-3.0-realtime-plus",
            "backend": "gpt-6-astra",
            "judge": "gpt-6-astra",
            "reasoning_effort": "low",
        },
        "seed_cases": selected,
        "eligible": rows,
        "excluded": excluded,
        "source": json.loads((source / "revision.json").read_text()),
        "adapter_files": freeze_files(Path(__file__).parent),
        "replay_wrapper_sha256": digest(
            Path(__file__).resolve().parents[2] / "scripts/gaia2_replay_export.py"
        ),
        "dataset_files": freeze_files(output / "dataset"),
        "profile_path": str(args.env_file.resolve()),
        "profile": profile_metadata(args.env_file),
        "tracks": ["audio"] if windowed else list(TRACKS),
        "max_concurrency": 4,
        "initial_concurrency": 1,
        "infra_retry_limit": 1,
        "model_retry_limit": 0,
    }
    write_json(output / "manifest.json", manifest)
    write_json(
        output / "freeze.json", {"manifest_sha256": digest(output / "manifest.json")}
    )
    (output / "selected-ids.txt").write_text(
        "".join(r["source_id"] + "\n" for r in selected)
    )
    import yaml

    for track in manifest["tracks"]:
        job = {
            "job_name": f"{output.name}-{track}",
            "jobs_dir": str(output / "jobs"),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
            "orchestrator": {"type": "local"},
            "environment": {"type": "docker", "delete": True},
            "agents": [
                {
                    "import_path": "adapters.gaia2.qwen_audio_agent:Gaia2QwenAudioAgent",
                    "model_name": "openai/gpt-6-astra",
                    "kwargs": {
                        "track": track,
                        "protocol_version": manifest["protocol"],
                        "scenario_timeout_sec": 2400,
                        "env_file": str(args.env_file.resolve()),
                    },
                }
            ],
            "datasets": [{"path": str(output / "dataset")}],
        }
        (output / f"{track}.yaml").write_text(yaml.safe_dump(job, sort_keys=False))
    print(
        json.dumps(
            {
                "campaign": str(output),
                "eligible": len(rows),
                "selected": len(selected),
                "base_runs": len(selected) * len(manifest["tracks"]),
            }
        )
    )


def verify_freeze(campaign):
    manifest = json.loads((campaign / "manifest.json").read_text())
    expected = json.loads((campaign / "freeze.json").read_text())["manifest_sha256"]
    if digest(campaign / "manifest.json") != expected:
        raise ValueError("Manifest changed after freeze")
    if (
        manifest.get("replay_wrapper_sha256") is not None
        and digest(
            Path(__file__).resolve().parents[2] / "scripts/gaia2_replay_export.py"
        )
        != manifest["replay_wrapper_sha256"]
    ):
        raise ValueError("Replay wrapper changed after freeze")
    if freeze_files(Path(__file__).parent) != manifest["adapter_files"]:
        raise ValueError("Adapter changed after freeze; create a new campaign")
    if freeze_files(campaign / "dataset") != manifest["dataset_files"]:
        raise ValueError("Packaged sources changed after freeze")
    if profile_metadata(Path(manifest["profile_path"])) != manifest["profile"]:
        raise ValueError(
            "Model profile changed after freeze; credential rotation alone is allowed"
        )
    for row in manifest["eligible"]:
        if digest(Path(row["audio_path"])) != row["sha256"]:
            raise ValueError(f"Audio changed: {row['source_id']}")
    return manifest


def results(jobs):
    rows = []
    for path in sorted(jobs.rglob("agent/gaia2_qwen_audio_result.json")):
        row = json.loads(path.read_text())
        row["artifact"] = str(path.resolve())
        rows.append(row)
    return rows


def audit(args):
    manifest = verify_freeze(args.campaign)
    rows = results(args.jobs)
    selection = {r["source_id"]: r for r in manifest["seed_cases"]}
    chosen = []
    for config in CONFIGS:
        candidates = [
            r
            for r in rows
            if r.get("track") == "audio" and r.get("audio", {}).get("config") == config
        ]
        candidates.sort(key=lambda r: r["audio"]["source_id"])
        chosen.extend(candidates[:4])
    for row in rows:
        if (
            row.get("status") != "completed"
            or row.get("timing", {}).get("final_formal_delivery_sec") is None
        ) and row not in chosen:
            chosen.append(row)
    entries = [
        {
            "source_id": r["audio"]["source_id"],
            "track": r["track"],
            "result": r["artifact"],
            "input_audio": selection.get(r["audio"]["source_id"], {}).get("audio_path"),
            "official_success": r.get("success"),
            "review": {
                "delivery_mapping_correct": None,
                "termination_correct": None,
                "interference_preserved": None,
                "semantic_agreement": None,
                "all_audible_content_reviewed": None,
                "task_and_quality_metrics_separate": None,
                "notes": "",
            },
        }
        for r in chosen
    ]
    target = args.campaign / "human-audit.json"
    if target.exists():
        raise ValueError("Audit already exists; preserve existing human labels")
    write_json(
        target,
        {
            "protocol": VERSION,
            "source": manifest["source"],
            "adapter_files": manifest["adapter_files"],
            "entries": entries,
        },
    )
    print(f"{len(entries)} audit entries written to {target}")


def load_audit(campaign):
    path = campaign / "human-audit.json"
    return json.loads(path.read_text()) if path.exists() else {}


def audit_digest(campaign):
    path = campaign / "human-audit.json"
    return digest(path) if path.exists() else None


def check_gate(manifest, rows, evidence, audit, tracks=TRACKS):
    failures = []
    expected = {(r["source_id"], t) for r in manifest["seed_cases"] for t in tracks}
    counts = Counter(
        (r.get("audio", {}).get("source_id"), r.get("track")) for r in rows
    )
    if set(counts) != expected or any(n != 1 for n in counts.values()):
        failures.append(
            f"Need exactly {len(expected)} first attempts for the frozen scenarios and selected tracks"
        )
    infra = sum(r.get("status") == "infrastructure_error" for r in rows)
    if not rows or infra / len(rows) > 0.01:
        failures.append("First-attempt infrastructure error rate exceeds 1%")
    if any(r.get("measurement_valid") is not True for r in rows):
        failures.append("Unresolved invalid or unaudited measurements")
    if any(r.get("protocol") != manifest.get("protocol", VERSION) for r in rows):
        failures.append("Protocol version mismatch")
    required = [
        "deterministic_controls",
        "trace_replay",
        "reference_isolation",
        "delivery_mapping",
        "termination",
        "model_profile",
        "concurrency_1_2_4",
        "infra_recovery",
    ]
    if tuple(tracks) == ("audio",):
        required.remove("concurrency_1_2_4")
    for name in required:
        item = evidence.get(name, {})
        if item.get("passed") is not True or not item.get("artifact"):
            failures.append(f"Missing passing evidence: {name}")
        elif not Path(item["artifact"]).is_file() or digest(
            Path(item["artifact"])
        ) != item.get("sha256"):
            failures.append(f"Evidence artifact missing or changed: {name}")
    if evidence.get("deterministic_controls", {}).get("count", 0) < 20:
        failures.append("Need at least 20 deterministic controls")
    if manifest.get("protocol") == "gaia2-qwen-audio/4":
        for name in ("observation_window", "audible_projection", "no_boundary_model"):
            item = evidence.get(name, {})
            if (
                item.get("passed") is not True
                or not item.get("artifact")
                or not Path(item["artifact"]).is_file()
                or digest(Path(item["artifact"])) != item.get("sha256")
            ):
                failures.append(f"Missing verified v4 evidence: {name}")
        if any(r.get("audible_transcript_complete") is not True for r in rows):
            failures.append("Incomplete audible projection")
    else:
        boundary = evidence.get(
            "boundary_stability", evidence.get("boundary_calibration", {})
        )
        if (
            boundary.get("passed") is not True
            or boundary.get("examples", 0) < 60
            or boundary.get("validation_mode") != "automatic_stability"
            or boundary.get("resolved_examples") != boundary.get("examples")
            or boundary.get("human_validated") is not False
        ):
            failures.append(
                "Automatic dialogue boundary stability check has not passed"
            )
    calibration = evidence.get("judge_calibration", {})
    if (
        calibration.get("positive", 0) < 15
        or calibration.get("negative", 0) < 15
        or calibration.get("repeats") != 3
        or calibration.get("agreement", 0) < 0.95
        or calibration.get("systematic_bias") is not False
    ):
        failures.append("Judge calibration has not passed")
    if manifest.get("review_policy") != "automatic":
        if audit.get("source") != manifest.get("source") or audit.get(
            "adapter_files"
        ) != manifest.get("adapter_files"):
            failures.append("Human audit belongs to a different source freeze")
        entries = audit.get("entries", [])
        audit_ids = [(e["source_id"], e["track"]) for e in entries]
        if len(audit_ids) != len(set(audit_ids)):
            failures.append("Human audit contains duplicate cases")
        categories = Counter(
            next(
                (
                    r["config"]
                    for r in manifest["seed_cases"]
                    if r["source_id"] == e["source_id"]
                ),
                "",
            )
            for e in entries
            if e["track"] == "audio"
        )
        if any(categories[c] < 4 for c in CONFIGS):
            failures.append("Human audit needs four audio cases per category")
        if not entries or any(
            e["review"].get(key) is not True
            for e in entries
            for key in [
                "delivery_mapping_correct",
                "termination_correct",
                "interference_preserved",
                "all_audible_content_reviewed",
                "task_and_quality_metrics_separate",
            ]
        ):
            failures.append("Unresolved human protocol audit")
        if (
            not entries
            or sum(e["review"].get("semantic_agreement") is True for e in entries)
            / len(entries)
            < 0.95
        ):
            failures.append("Human semantic agreement below 95%")
    return {
        "passed": not failures,
        "failures": failures,
        "review_policy": manifest.get("review_policy", "human"),
        "human_semantic_validation_required": manifest.get("review_policy")
        != "automatic",
        "boundary_semantic_accuracy": None,
        "audio_transcript_semantics_verified": False,
        "scheduled_denominator": len(expected),
        "infrastructure_first_attempts": infra,
    }


def expand(args):
    """Materialize only unrun audio cases after the pilot quality gate."""
    import pyarrow.parquet as pq
    import yaml

    manifest = verify_freeze(args.campaign)
    report = check_gate(
        manifest,
        results(args.jobs),
        json.loads(args.evidence.read_text()),
        load_audit(args.campaign),
    )
    if not report["passed"]:
        raise ValueError("Expansion blocked: " + "; ".join(report["failures"]))
    seed_ids = {r["source_id"] for r in manifest["seed_cases"]}
    remaining = {
        r["source_id"]: r
        for r in manifest["eligible"]
        if r["source_id"] not in seed_ids
    }
    output = args.campaign / "remaining-dataset"
    if output.exists():
        raise ValueError("Expansion already prepared; do not replace frozen tasks")
    adapter = Gaia2Adapter(output)
    source = (args.campaign / "dataset/.qwen-source").resolve()
    generated = set()
    for config in CONFIGS:
        for shard in sorted((args.dataset / config).glob("validation-*.parquet")):
            for record in pq.read_table(shard).to_pylist():
                if record["id"] not in remaining:
                    continue
                task = Gaia2Adapter.metadata_from_record(
                    record=record, config=config, split="validation"
                )
                row = remaining[task.source_id]
                if (
                    hashlib.sha256(task.scenario_json.encode()).hexdigest()
                    != row["scenario_sha256"]
                ):
                    raise ValueError("Source scenario changed after freeze")
                asset = find_asset(
                    task, audio_dir=None, manifest={(config, task.source_id): row}
                )
                generate_audio_task(adapter, task, asset, source)
                generated.add(task.source_id)
    if generated != set(remaining):
        raise ValueError("Missing source scenarios for expansion")
    job = yaml.safe_load((args.campaign / "audio.yaml").read_text())
    job.update(job_name=args.campaign.name + "-audio-remaining", n_concurrent_trials=4)
    job["datasets"] = [{"path": str(output.resolve())}]
    (args.campaign / "audio-remaining.yaml").write_text(
        yaml.safe_dump(job, sort_keys=False)
    )
    write_json(
        args.campaign / "expansion.json",
        {
            "reused_audio_cases": len(seed_ids),
            "remaining": len(generated),
            "total_audio_cases": len(manifest["eligible"]),
            "manifest_sha256": digest(args.campaign / "manifest.json"),
            "files": freeze_files(output),
        },
    )
    print(
        f"Prepared {len(generated)} remaining audio cases; reuse {len(seed_ids)} frozen pilot audio cases."
    )


def run_campaign(args):
    """Execute frozen jobs; one fresh retry only for recorded infrastructure errors."""
    import os
    import subprocess
    import sys

    import yaml

    manifest = verify_freeze(args.campaign)
    if manifest.get("protocol") == VERSION:
        from adapters.gaia2.campaign_runner import run

        return run(
            args.campaign,
            "audio" if args.stage == "pilot" else args.stage,
            getattr(args, "services_restored", False),
            args.concurrency,
        )
    if not getattr(args, "services_restored", False):
        raise ValueError(
            "Services remain unverified; explicitly confirm restoration before launching"
        )
    if args.stage == "remaining":
        gate = json.loads((args.campaign / "gate.json").read_text())
        if gate.get("passed") is not True:
            raise ValueError("The pilot quality gate must pass before a full run")
        if (
            gate.get("manifest_sha256") != digest(args.campaign / "manifest.json")
            or gate.get("audit_sha256") != audit_digest(args.campaign)
            or gate.get("evidence_sha256") != digest(Path(gate["evidence_path"]))
        ):
            raise ValueError("Gate evidence changed; run the gate again")
        expansion = json.loads((args.campaign / "expansion.json").read_text())
        if expansion["manifest_sha256"] != digest(args.campaign / "manifest.json"):
            raise ValueError("Expansion belongs to a different freeze")
        if freeze_files(args.campaign / "remaining-dataset") != expansion["files"]:
            raise ValueError("Expansion tasks changed")
        tracks = ["audio-remaining"]
    else:
        tracks = list(TRACKS)
    task_names = {r["source_id"]: r["task_name"] for r in manifest["eligible"]}
    outcome = []
    for track in tracks:
        config = yaml.safe_load((args.campaign / f"{track}.yaml").read_text())
        job = Path(config["jobs_dir"]) / config["job_name"]
        if job.exists():
            raise ValueError(f"Preserve existing job {job}; do not silently rerun it")
        config["n_concurrent_trials"] = args.concurrency
        launch = args.campaign / f"launch-{track}.yaml"
        launch.write_text(yaml.safe_dump(config, sort_keys=False))
        completed = subprocess.run(
            [sys.executable, "-m", "harbor.cli.main", "run", "-c", str(launch)],
            env={**os.environ, "DOCKER_BUILDKIT": "0"},
            check=False,
        )
        rows = results(job)
        infra = [r for r in rows if r.get("status") == "infrastructure_error"]
        record = {
            "track": track,
            "first_job": str(job),
            "exit_code": completed.returncode,
            "recorded_trials": len(rows),
            "infrastructure_failures": len(infra),
            "retry_job": None,
        }
        if infra:
            retry = json.loads(json.dumps(config))
            retry["job_name"] += "-infra-retry-1"
            retry["jobs_dir"] = str((args.campaign / "retries").resolve())
            retry["datasets"][0]["task_names"] = [
                task_names[r["audio"]["source_id"]] for r in infra
            ]
            retry_config = args.campaign / f"retry-{track}.yaml"
            retry_config.write_text(yaml.safe_dump(retry, sort_keys=False))
            retried = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "harbor.cli.main",
                    "run",
                    "-c",
                    str(retry_config),
                ],
                env={**os.environ, "DOCKER_BUILDKIT": "0"},
                check=False,
            )
            record.update(
                retry_job=str(Path(retry["jobs_dir"]) / retry["job_name"]),
                retry_exit_code=retried.returncode,
            )
        outcome.append(record)
        write_json(args.campaign / f"{args.stage}-execution.json", outcome)
        # Unknown setup failures are never silently retried or dropped from a denominator.
        if completed.returncode and not rows:
            raise RuntimeError(
                "Job failed before producing trial evidence; inspect its logs"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument(
        "--protocol", choices=[VERSION, "gaia2-qwen-audio/4"], default=VERSION
    )
    prep.add_argument("--selection-limit", type=int)
    prep.add_argument("--reuse-selection", type=Path)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--dataset", type=Path, default=Path("../gaia2"))
    prep.add_argument("--audio", type=Path, default=Path("../wav_export"))
    prep.add_argument("--qwen", type=Path, default=Path("../qwen-audio-agent"))
    prep.add_argument(
        "--env-file",
        type=Path,
        default=Path(
            "/shared/chaolei.liu/.config/qwaudio/gaia2-evaluation-ali-speech-gpt.env"
        ),
    )
    for name in ["audit", "gate", "audio-gate", "expand"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--campaign", type=Path, required=True)
        cmd.add_argument("--jobs", type=Path, required=True)
        if name == "expand":
            cmd.add_argument("--dataset", type=Path, default=Path("../gaia2"))
        if name in {"gate", "audio-gate", "expand"}:
            cmd.add_argument("--evidence", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--services-restored", action="store_true")
    run.add_argument("--campaign", type=Path, required=True)
    run.add_argument(
        "--stage",
        choices=[
            "regression",
            "smoke10",
            "audio",
            "controls",
            "load",
            "pilot",
            "remaining",
        ],
        default="pilot",
    )
    run.add_argument("--concurrency", choices=[1, 2, 4], type=int, default=1)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "audit":
        audit(args)
    elif args.command == "expand":
        expand(args)
    elif args.command == "run":
        run_campaign(args)
    else:
        manifest = verify_freeze(args.campaign)
        report = check_gate(
            manifest,
            results(args.jobs),
            json.loads(args.evidence.read_text()),
            load_audit(args.campaign),
            tracks=("audio",) if args.command == "audio-gate" else TRACKS,
        )
        report.update(
            manifest_sha256=digest(args.campaign / "manifest.json"),
            audit_sha256=audit_digest(args.campaign),
            evidence_path=str(args.evidence.resolve()),
            evidence_sha256=digest(args.evidence),
        )
        write_json(
            args.campaign
            / (
                "audio-pilot-gate.json" if args.command == "audio-gate" else "gate.json"
            ),
            report,
        )
        print(json.dumps(report, indent=2))
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

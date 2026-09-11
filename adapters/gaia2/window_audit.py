"""Synchronous first-attempt audit before a v4 scheduler admits another task."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from adapters.gaia2.qwen_audio_template.environment.runner.observation import (
    project_audio,
)


def verify_integrity(root):
    root = root.resolve()
    manifest = json.loads((root / "integrity.json").read_text())["files"]
    actual = {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.name != "integrity.json"
    }
    if actual != set(manifest):
        raise ValueError("Artifact inventory differs from integrity manifest")
    for name, expected in manifest.items():
        path = (root / name).resolve()
        if (
            not path.is_relative_to(root)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Artifact hash mismatch")
    return len(manifest)


def audit_trial(campaign, row, job, result):
    output = campaign / "replay"
    output.mkdir(exist_ok=True)
    report = {"source_id": row["source_id"], "passed": False}
    try:
        roots = list(job.glob("*/artifacts/evaluation/results"))
        if len(roots) != 1:
            raise ValueError("Expected exactly one archived trial")
        root = roots[0].resolve()
        report["artifact_count"] = verify_integrity(root)
        if json.loads((root / "result.json").read_text()) != result:
            raise ValueError("Archived result differs from primary result")
        evidence_files = [root / "integrity.json", root / "projection.json"]
        projection = project_audio(root)
        saved = json.loads((root / "projection.json").read_text())
        if any(saved.get(k) != v for k, v in projection.items()):
            raise ValueError("Saved projection does not match audible artifacts")
        if not projection["audible_transcript_complete"]:
            raise ValueError("Audible transcript incomplete")
        if result.get("measurement_valid") is not True:
            raise ValueError("Trial measurement is not valid")
        if result["status"] == "completed":
            repository = Path(__file__).resolve().parents[2]
            source = (
                campaign / "dataset" / row["task_name"] / "environment/scenario.json"
            )
            destination = output / f"{row['source_id']}.json"
            command = [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{repository / 'adapters/gaia2/qwen_audio_template/environment/runner'}:/opt/runner:ro",
                "-v",
                f"{repository / 'scripts/gaia2_replay_export.py'}:/replay_export.py:ro",
                "-v",
                f"{source.resolve()}:/source.json:ro",
                "-v",
                f"{root}:/evaluation/results:ro",
                "-v",
                f"{output.resolve()}:/replay",
                "hearme-gaia2-are:dev",
                "python",
                "/replay_export.py",
                "/evaluation/results",
                "--source-scenario",
                "/source.json",
                "--output",
                f"/replay/{destination.name}",
            ]
            replay = subprocess.run(
                command, capture_output=True, text=True, timeout=180, check=False
            )
            destination.with_suffix(".log").write_text(replay.stdout + replay.stderr)
            if replay.returncode:
                raise ValueError("Official offline replay failed")
            evidence = json.loads(destination.read_text())
            if (
                not evidence["passed"]
                or evidence["success"] != result["success"]
                or evidence["fresh_model_calls"]
            ):
                raise ValueError("Official replay mismatch")
            report["official_replay"] = True
            evidence_files.append(destination)
        else:
            report["official_replay"] = "not_applicable_to_noncompleted_status"
        report["evidence_sha256"] = {
            str(path.resolve().relative_to(campaign.resolve())): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in evidence_files
        }
        report["passed"] = True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    (output / f"{row['source_id']}.audit.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    return report


def verify_saved_audit(campaign, entry):
    """Revalidate saved proof on resume, without issuing any new judge request."""
    path = campaign / "replay" / f"{entry['source_id']}.audit.json"
    saved = json.loads(path.read_text())
    if saved != entry.get("audit") or saved.get("passed") is not True:
        raise ValueError("Prior audit proof differs from ledger")
    root = next(Path(entry["job"]).glob("*/artifacts/evaluation/results"))
    verify_integrity(root)
    result = json.loads((root / "result.json").read_text())
    if result != entry["result"]:
        raise ValueError("Audited result differs from ledger")
    for filename, expected in saved.get("evidence_sha256", {}).items():
        evidence = (campaign / filename).resolve()
        if (
            not evidence.is_relative_to(campaign.resolve())
            or hashlib.sha256(evidence.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Prior audit evidence changed")
    if not saved.get("evidence_sha256"):
        raise ValueError("Audit has no evidence fingerprints")

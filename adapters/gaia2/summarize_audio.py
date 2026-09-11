"""Summarize a Qwen audio Harbor job without hiding infrastructure failures."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def summarize(job: Path, coverage: dict[str, Any] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for trial in sorted(job.iterdir()):
        if not trial.is_dir():
            continue
        payload_path = trial / "agent/gaia2_qwen_audio_result.json"
        if not payload_path.exists():
            payload_path = trial / "verifier/gaia2_qwen_audio_result.json"
        trial_result = trial / "result.json"
        if payload_path.exists():
            payload = json.loads(payload_path.read_text())
            status = payload["status"]
            success = (
                status == "completed"
                and payload.get("success") is True
                and payload.get("measurement_valid", True) is True
            )
            category = payload.get("audio", {}).get("config", "unknown")
        elif trial_result.exists():
            status, success, category = "infrastructure_error", False, "unknown"
            payload = {}
        else:
            continue
        rows.append(
            {
                "trial": trial.name,
                "config": category,
                "status": status,
                "success": success,
                "track": payload.get("track", "prototype"),
                "protocol": payload.get("protocol"),
                "timing": payload.get("timing", {}),
                "measurement_valid": payload.get(
                    "measurement_valid",
                    status in {"completed", "protocol_failure", "execution_timeout"},
                ),
                "failure_cause": payload.get("failure_cause"),
            }
        )
    counts = Counter(row["status"] for row in rows)
    categories = defaultdict(
        lambda: {"executed": 0, "passed": 0, "infrastructure_errors": 0}
    )
    for row in rows:
        category = categories[row["config"]]
        category["executed"] += 1
        category["passed"] += int(row["success"])
        category["infrastructure_errors"] += int(
            row["status"] == "infrastructure_error"
        )
    selected = coverage.get("selected", len(rows)) if coverage else len(rows)
    passed = sum(int(row["success"] is True) for row in rows)
    graded = sum(
        row["measurement_valid"]
        and row["status"] in {"completed", "execution_timeout", "protocol_failure"}
        for row in rows
    )
    timing = {}
    for track in sorted({r["track"] for r in rows}):
        timing[track] = {}
        for key in [
            "input_audio_sec",
            "first_application_action_sec",
            "final_formal_delivery_sec",
            "elapsed_sec",
            "raw_wall_sec",
            "excluded_pause_sec",
        ]:
            values = sorted(
                r["timing"][key]
                for r in rows
                if r["track"] == track and r["timing"].get(key) is not None
            )
            timing[track][key] = {
                "observed": len(values),
                "missing": sum(r["track"] == track for r in rows) - len(values),
                "p50": statistics.median(values) if values else None,
                "p95": values[min(len(values) - 1, int((len(values) - 1) * 0.95))]
                if values
                else None,
            }

    return {
        "benchmark": "gaia2-qwen-audio",
        "selected": selected,
        "timing_by_track": timing,
        "scored": graded,
        "measurement_invalid": sum(
            row["measurement_valid"] is False and row["status"] != "user_aborted"
            for row in rows
        ),
        "user_aborted": counts["user_aborted"],
        "failure_causes": dict(
            Counter(row["failure_cause"] for row in rows if row["failure_cause"])
        ),
        "executed": len(rows),
        "not_executed": max(0, selected - len(rows)),
        "passed": passed,
        "statuses": dict(counts),
        "pass_rate_selected": passed / selected if selected else None,
        "pass_rate_graded": passed / graded if graded else None,
        "by_config": dict(categories),
        "trials": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    coverage = json.loads(args.coverage.read_text()) if args.coverage else None
    text = json.dumps(summarize(args.job_dir, coverage), indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()

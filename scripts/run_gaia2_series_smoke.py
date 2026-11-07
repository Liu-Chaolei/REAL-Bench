#!/usr/bin/env python3
"""Run the frozen ten-task sample through ordinary Qwen voice evaluation."""

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path


def main():
    """Preserve every first attempt and stop on infrastructure failures."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selection",
        type=Path,
        default=root
        / "data/simulations/gaia2-series-smoke-preflight-20261106-105704/selection.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "integrations/qwen_audio_agent/config.gaia2.example.json",
    )
    parser.add_argument(
        "--save-to",
        default="gaia2-series-live-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    entries = selection["tasks"]
    entries.sort(key=lambda row: not row["id"].startswith("0353_"))
    output = root / "data/simulations" / args.save_to
    output.mkdir(parents=True, exist_ok=False)
    (output / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n"
    )
    attempts = []
    for entry in entries:
        task_id = entry["id"]
        child = f"{args.save_to}/task_{task_id}"
        command = [
            str(root / "integrations/qwen_audio_agent/run_hpc.sh"),
            "smoke",
            "--domain",
            "gaia2",
            "--config",
            str(args.config),
            "--task-ids",
            task_id,
            "--seed",
            str(selection["seed"]),
            "--max-concurrency",
            "1",
            "--max-seconds",
            str(args.max_seconds),
            "--save-to",
            child,
        ]
        print(f"Starting {task_id}", flush=True)
        with (output / f"task_{task_id}.log").open("w") as log:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, cwd=root
            )
        result_path = root / "data/simulations" / child / "results.json"
        index = (
            json.loads(result_path.read_text()).get("simulation_index", [])
            if result_path.exists()
            else []
        )
        attempt = {
            "task_id": task_id,
            "category": entry["category"],
            "exit_code": result.returncode,
            "results": str(result_path),
            "simulation_index": index,
        }
        attempts.append(attempt)
        failed = (
            result.returncode != 0
            or len(index) != 1
            or any(
                row.get("termination_reason") in {"infrastructure_error", "user_error"}
                or row.get("evaluation_status") == "failed"
                for row in index
            )
        )
        report = {
            "status": "blocked" if failed else "running",
            "attempts": attempts,
            "scheduled_count": len(entries),
            "not_run_ids": [
                row["id"]
                for row in entries
                if row["id"] not in {attempt["task_id"] for attempt in attempts}
            ],
            "automatic_retries": 0,
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "exit_code": result.returncode,
                    "infrastructure_or_evaluation_failure": failed,
                }
            ),
            flush=True,
        )
        if failed:
            raise SystemExit(1)
    report["status"] = "completed"
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Completed sample: {output}", flush=True)


if __name__ == "__main__":
    main()

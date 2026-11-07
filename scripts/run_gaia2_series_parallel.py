#!/usr/bin/env python3
"""Run all unfinished frozen GAIA2 series tasks concurrently."""

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock


def main():
    """Keep one independent tau process per task and preserve every result."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selection",
        type=Path,
        default=root
        / "data/simulations/gaia2-series-smoke-preflight-20261106-105704/selection.json",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--save-to",
        default="gaia2-series-live-parallel-"
        + datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--concurrency", type=int, default=9)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    entries = selection["tasks"]
    output = root / "data/simulations" / args.save_to
    output.mkdir(parents=True, exist_ok=False)
    (output / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n"
    )
    first_success = "0353_ovfqd71tby5xjef8fqs25665mbosdjei"
    entries = [entry for entry in entries if entry["id"] != first_success]
    attempts = []
    lock = Lock()

    def run_one(entry):
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
        log_path = output / f"task_{task_id}.log"
        with log_path.open("w") as log:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, cwd=root
            )
        result_path = root / "data/simulations" / child / "results.json"
        index = (
            json.loads(result_path.read_text()).get("simulation_index", [])
            if result_path.exists()
            else []
        )
        failed = (
            result.returncode != 0
            or len(index) != 1
            or any(
                row.get("termination_reason") in {"infrastructure_error", "user_error"}
                or row.get("evaluation_status") == "failed"
                for row in index
            )
        )
        return {
            "task_id": task_id,
            "category": entry["category"],
            "exit_code": result.returncode,
            "results": str(result_path),
            "simulation_index": index,
            "failed": failed,
            "log": str(log_path),
        }

    def save(status):
        attempted = {item["task_id"] for item in attempts}
        report = {
            "status": status,
            "parallel": True,
            "concurrency": args.concurrency,
            "scheduled_count": len(entries),
            "automatic_retries": 0,
            "attempts": sorted(attempts, key=lambda item: item["task_id"]),
            "not_run_ids": [
                item["id"] for item in entries if item["id"] not in attempted
            ],
            "previous_successes": [first_success],
        }
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_one, entry): entry for entry in entries}
        for future in as_completed(futures):
            result = future.result()
            with lock:
                attempts.append(result)
                save("running")
            print(
                json.dumps({k: result[k] for k in ("task_id", "exit_code", "failed")}),
                flush=True,
            )
    save("completed")
    print(f"Completed parallel sample: {output}", flush=True)


if __name__ == "__main__":
    main()

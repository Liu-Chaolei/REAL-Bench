#!/usr/bin/env python3
"""Run a frozen REAL-metrics sample through the ordinary tau voice runner."""

import argparse
import json
import subprocess
from pathlib import Path


def main():
    """Preserve first attempts and stop admission on infrastructure/grading errors."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--agent-config",
        type=Path,
        default=root
        / "integrations/qwen_audio_agent/config.gaia2.metrics.example.json",
    )
    parser.add_argument(
        "--metrics-config", type=Path, default=root / "examples/real-metrics.yaml"
    )
    parser.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "selection.json").write_text(json.dumps(selection, indent=2) + "\n")
    attempts = []
    for entry in selection["tasks"]:
        child = output / entry["id"]
        save_to = child.relative_to(root / "data/simulations").as_posix()
        command = [
            str(root / "integrations/qwen_audio_agent/run_hpc.sh"),
            "smoke",
            "--domain",
            "gaia2",
            "--config",
            str(args.agent_config.resolve()),
            "--metrics-config",
            str(args.metrics_config.resolve()),
            "--task-ids",
            entry["id"],
            "--seed",
            str(selection["seed"]),
            "--max-concurrency",
            "1",
            "--max-seconds",
            str(args.max_seconds),
            "--save-to",
            save_to,
        ]
        print("Starting", entry["id"], flush=True)
        with (output / (entry["id"] + ".log")).open("w") as log:
            process = subprocess.run(
                command, cwd=root, stdout=log, stderr=subprocess.STDOUT
            )
        meta = child / "results.json"
        index = (
            json.loads(meta.read_text()).get("simulation_index", [])
            if meta.exists()
            else []
        )
        diagnostic = list(child.glob("artifacts/*/*/diagnostics/latest.json"))
        summaries = [json.loads(p.read_text()) for p in diagnostic]
        fatal = (
            process.returncode != 0
            or len(index) != 1
            or any(
                r.get("termination_reason")
                in {"infrastructure_error", "user_error", "unexpected_error"}
                or r.get("evaluation_status") == "failed"
                for r in index
            )
        )
        fatal |= not summaries or any(
            m.get("status") in {"grader_error", "error"}
            for r in summaries
            for m in r["summary"]["measures"].values()
        )
        attempts.append(
            {
                **entry,
                "exit_code": process.returncode,
                "simulation_index": index,
                "diagnostics": summaries,
            }
        )
        report = {
            "status": "stopped_on_invalid_measurement" if fatal else "running",
            "attempts": attempts,
            "selected_count": len(selection["tasks"]),
            "not_run_ids": [r["id"] for r in selection["tasks"][len(attempts) :]],
        }
        if not report["not_run_ids"] and not fatal:
            report["status"] = "completed"
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(output / "report.json")
        if fatal:
            print(
                "Stopped after invalid measurement; original attempt preserved.",
                flush=True,
            )
            break
    print("Report:", output / "report.json", flush=True)


if __name__ == "__main__":
    main()

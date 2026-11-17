"""Run integrated Gaia2 async tasks on Qwen through the ordinary task entry point."""

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from validate_qwen_persistent_run import validate

from tau2.data_model.simulation import Results
from tau2.persistent_tasks.config import PersistentTaskConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", required=True)
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--run-prefix", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    data = repo / "data/tau2/domains/gaia2"
    manifest = json.loads((data / "manifest.json").read_text())
    tasks = {
        task["id"]: task
        for task in json.loads((data / "tasks.json").read_text())
        if task["scenario"].get("async_interaction") is not None
    }
    aliases = manifest.get("async_legacy_ids", {})
    args.cases = [aliases.get(case, case) for case in args.cases]
    if not args.run_prefix.replace("-", "").replace("_", "").isalnum():
        parser.error("Use a simple run prefix without paths")
    if any(case not in tasks for case in args.cases):
        parser.error("Unknown Gaia2 async task ID")
    if len(set(args.cases)) != len(args.cases):
        parser.error("Duplicate case IDs would overwrite one another")
    configs = {
        case: PersistentTaskConfig.model_validate(
            tasks[case]["scenario"]["async_interaction"]
        )
        for case in args.cases
    }
    outputs = {
        case: repo / "data/simulations" / f"{args.run_prefix}-{case}"
        for case in args.cases
    }
    if any(path.exists() for path in outputs.values()):
        parser.error(
            "Output paths already exist; use a new run prefix to preserve attempts"
        )
    results = {}

    def run(case):
        output = outputs[case]
        output.mkdir(parents=True)
        log_path = output / "acceptance-run.log"
        command = [
            str(repo / "integrations/qwen_audio_agent/run_hpc.sh"),
            "smoke",
            "--domain",
            "gaia2",
            "--config",
            str(args.config.resolve()),
            "--task-ids",
            case,
            "--max-seconds",
            str(args.max_seconds),
            "--save-to",
            output.name,
            "--max-concurrency",
            "1",
        ]
        with log_path.open("w") as stream:
            os.chmod(log_path, 0o600)
            completed = subprocess.run(
                command, cwd=repo, stdout=stream, stderr=subprocess.STDOUT
            )
        report = {
            "scenario_id": case,
            "status": "execution_failed",
            "measurement_verified": False,
            "expected_behavior_passed": False,
            "returncode": completed.returncode,
            "log": str(log_path),
        }
        try:
            loaded = Results.load(output / "results.json")
            simulations = list(loaded.simulations)
            if len(simulations) != 1:
                raise ValueError(
                    "A one-trial acceptance requires exactly one saved simulation"
                )
            simulation = simulations[0]
            evidence = (
                output
                / "artifacts"
                / f"task_{case}"
                / f"sim_{simulation.id}"
                / "diagnostics/persistent_tasks.json"
            )
            availability = (simulation.info or {}).get("async_availability")
            if availability:
                report.update(
                    status=availability["status"],
                    reasons=[availability["reason"]],
                    native_reward=None,
                    task_id=case,
                    scenario_id=case,
                )
            else:
                report = validate(
                    evidence, output / "simulations" / f"{simulation.id}.json"
                )
                if simulation.evaluation_status != "completed":
                    report.update(
                        status="evaluation_failed",
                        measurement_verified=False,
                        expected_behavior_passed=False,
                    )
            report["returncode"] = completed.returncode
        except Exception as error:
            report.update(error_type=type(error).__name__, error=str(error))
        report_path = output / "acceptance.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return case, report, report_path

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, case) for case in args.cases]
        for future in as_completed(futures):
            case, report, path = future.result()
            results[case] = {
                "status": report["status"],
                "report": str(path),
                "measurement_verified": report["measurement_verified"],
                "expected_behavior_passed": report["expected_behavior_passed"],
                "config_sha256": configs[case].digest(),
                "evidence_sha256": report.get("evidence_sha256"),
            }
            summary = repo / "data/simulations" / f"{args.run_prefix}-summary.json"
            summary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
            print(case, report["status"], flush=True)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

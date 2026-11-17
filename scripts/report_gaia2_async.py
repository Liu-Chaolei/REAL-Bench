"""Summarize integrated Gaia2 async tasks and preserved evaluation attempts."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def aggregate(data: Path, simulations: Path):
    manifest = json.loads((data / "manifest.json").read_text())
    tasks = [
        task
        for task in json.loads((data / "tasks.json").read_text())
        if task["scenario"].get("async_interaction") is not None
    ]
    attempts = defaultdict(list)
    for directory in sorted(simulations.iterdir()):
        if not directory.is_dir():
            continue
        path = directory / "acceptance.json"
        if not path.exists():
            continue
        report = json.loads(path.read_text())
        identifier = report.get("task_id") or report.get("scenario_id")
        attempts[identifier].append((path.stat().st_mtime_ns, path, report))
    cases = {}
    statuses = Counter()
    by_category = defaultdict(Counter)
    by_operation = defaultdict(Counter)
    by_state = defaultdict(Counter)
    for task in tasks:
        identifier = task["id"]
        scene = task["scenario"]
        metadata = scene["async_metadata"]
        config = scene["async_interaction"]
        candidates = attempts.get(identifier, [])
        path, report = (
            max(candidates, key=lambda row: row[0])[1:] if candidates else (None, {})
        )
        grading = report.get("grading") or {}
        status = report.get(
            "status",
            "not_run" if metadata["readiness"] == "ready" else "task_not_ready",
        )
        if status == "unverified":
            status = grading.get("status", status)
        cases[identifier] = {
            "status": status,
            "readiness": metadata["readiness"],
            "reasons": report.get("reasons", metadata["reasons"]),
            "measurement_verified": report.get("measurement_verified", False),
            "expected_behavior_passed": report.get("expected_behavior_passed", False),
            "native_reward": grading.get("native_reward"),
            "ordinary_gaia2_reward": report.get("original_gaia2_reward"),
            "report": str(path) if path else None,
            "attempts": [
                str(row[1]) for row in sorted(candidates, key=lambda row: row[0])
            ],
            "coverage": grading.get("coverage", {}),
            "evidence_sha256": report.get("evidence_sha256"),
            "config_sha256": report.get("config_sha256"),
        }
        statuses[status] += 1
        by_category[metadata["source_category"]][status] += 1
        operation = (
            "association"
            if len(config["bindings"]) > 1
            else config["requests"][0]["operation"]
        )
        by_operation[operation][status] += 1
        for state in {request["state"] for request in config["requests"]}:
            by_state[state][status] += 1
    return {
        "version": "gaia2-async-report-v1",
        "population": len(tasks),
        "source_population": len(json.loads((data / "tasks.json").read_text()))
        - len(tasks),
        "status_counts": dict(statuses),
        "by_source_category": {key: dict(value) for key, value in by_category.items()},
        "by_operation": {key: dict(value) for key, value in by_operation.items()},
        "by_state": {key: dict(value) for key, value in by_state.items()},
        "measurement_verified": sum(
            row["measurement_verified"] for row in cases.values()
        ),
        "expected_behavior_passed": sum(
            row["expected_behavior_passed"] for row in cases.values()
        ),
        "historical_acceptance": manifest.get("async_historical_acceptance", {}),
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/tau2/domains/gaia2"))
    parser.add_argument("--simulations", type=Path, default=Path("data/simulations"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/simulations/gaia2-async-summary.json")
    )
    args = parser.parse_args()
    report = aggregate(args.data, args.simulations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(
        "# Gaia2 async evaluation\n\n"
        f"Definitions: {report['population']}; measurement verified: {report['measurement_verified']}; behavior passed: {report['expected_behavior_passed']}.\n\n"
        "Historical qpt attempts are preserved separately and are not counted as new task runs.\n\n"
        + "\n".join(
            f"- {status}: {count}" for status, count in report["status_counts"].items()
        )
        + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()

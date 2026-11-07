#!/usr/bin/env python3
"""Inspect and verify personally authored Gaia2 candidates without model calls."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def bootstrap(dataset: Path) -> None:
    """Select the sealed application runtime before importing tau2."""
    manifest = json.loads((dataset / "manifest.json").read_text())
    runtime = Path(manifest["manual_runtime"]).resolve()
    if not (runtime / "tau2/domains/gaia2/utils.py").exists():
        raise ValueError("Pinned runtime is missing")
    sys.path.insert(0, str(runtime))
    # Keep applications and verifiers pinned; load only the manual adapter from
    # this checkout. Its separate file hash is recorded on every acceptance.
    package = importlib.import_module("tau2.task_authoring.gaia2_series")
    adapter = (
        Path(__file__).resolve().parents[1] / "src/tau2/task_authoring/gaia2_series"
    )
    package.__path__.append(str(adapter))
    from tau2.task_authoring.gaia2_series.storage import source_tasks

    source_tasks(dataset)


def worker(dataset: Path, submission: dict) -> dict:
    from tau2.task_authoring.gaia2_series.manual import verify_submission

    return verify_submission(dataset, submission)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["next", "preflight", "verify", "status"])
    parser.add_argument(
        "--output", type=Path, required=True, help="Existing manual dataset"
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Compatibility argument; the manifest is authoritative",
    )
    parser.add_argument("--submission", type=Path, action="append", default=[])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--batch", default="manual-next")
    args = parser.parse_args()
    dataset = args.output.resolve()
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    bootstrap(dataset)
    from tau2.task_authoring.gaia2_series.manual import (
        current_status,
        next_tasks,
        preflight_submission,
    )
    from tau2.task_authoring.gaia2_series.pipeline import dataset_lock
    from tau2.task_authoring.gaia2_series.storage import write_json

    if args.command == "next":
        if not 1 <= args.limit <= 30:
            parser.error("next limit must be between 1 and 30")
        result = next_tasks(dataset, limit=args.limit)
    elif args.command == "status":
        result = current_status(dataset)
    elif args.command == "preflight":
        if not args.submission:
            parser.error("preflight needs --submission")
        result = []
        for path in args.submission:
            submission = json.loads(path.read_text())
            result.append(preflight_submission(dataset, submission))
    else:
        if not args.submission or not 1 <= args.workers <= 30:
            parser.error("verify needs --submission and workers between 1 and 30")
        if Path(args.batch).name != args.batch:
            parser.error("batch must be a filename label")
        submissions = [json.loads(p.read_text()) for p in args.submission]
        ids = [s["task_id"] for s in submissions]
        if len(ids) != len(set(ids)):
            parser.error("Only one submission per task is allowed")
        results = []
        with dataset_lock(dataset):
            with ProcessPoolExecutor(
                max_workers=min(args.workers, len(submissions))
            ) as pool:
                futures = {
                    pool.submit(worker, dataset, s): s["task_id"] for s in submissions
                }
                for future in as_completed(futures):
                    try:
                        value = future.result()
                    except Exception as error:
                        value = {
                            "task_id": futures[future],
                            "status": "submission_error",
                            "error": f"{type(error).__name__}: {error}",
                        }
                    results.append(value)
                    write_json(
                        dataset.parent / "manual-batches" / f"{args.batch}.json",
                        {"results": results, "model_calls": 0},
                    )
                    state = current_status(dataset)
                    write_json(dataset / "manual-state.json", state)
                    write_json(dataset / "manual-coverage-report.json", state)
                    print(json.dumps(value, ensure_ascii=False), flush=True)
        result = current_status(dataset)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run all Gaia2 source-evidence repair batches with resumable checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from repair_gaia2_unresolved import load_env_file, read_json, run_v2, write_json
from review_gaia2_references import load_config


def pending_batches(inputs: list[Path], out: Path, size: int) -> list[list[str]]:
    """Plan explicit task IDs so a stale batch cursor cannot leave holes."""
    pending = []
    for path in inputs:
        result_path = out / "results" / path.name
        result = read_json(result_path) if result_path.exists() else {}
        if result.get("evidence_review") != "source_replay_resolved":
            pending.append(path.stem)
    return [pending[index : index + size] for index in range(0, len(pending), size)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int)
    parser.add_argument(
        "--pending-only",
        action="store_true",
        help="Fill missing and legacy checkpoints by task ID; preserve already reviewed results",
    )
    args = parser.parse_args()
    if args.batch_size < 1 or args.concurrency < 1:
        parser.error("Batch size and concurrency must be positive")

    load_env_file(args.env_file)
    config = load_config(args.config)
    config.concurrency = max(1, args.concurrency)
    inputs = sorted((args.out / "inputs").glob("*.json"))
    stop = args.stop if args.stop is not None else len(inputs)
    progress_path = args.out / "batch-driver-progress.json"
    plans = (
        [
            (0, ids)
            for ids in pending_batches(
                inputs[args.start : stop], args.out, args.batch_size
            )
        ]
        if args.pending_only
        else [(start, None) for start in range(args.start, stop, args.batch_size)]
    )
    for start, ids in plans:
        summary = run_v2(
            v1_root=args.input_root,
            review_dir=args.review_dir,
            data_root=args.data_root,
            out=args.out,
            config=config,
            limit=None,
            offline=False,
            evidence_root=args.evidence_root,
            batch_start=start,
            batch_size=args.batch_size,
            resolve_sources=True,
            selected_task_ids=ids,
        )
        state = {
            "batch_start": start,
            "batch_size": args.batch_size,
            "stop": stop,
            "summary": summary,
            "selected_task_ids": ids,
            "concurrency": config.concurrency,
        }
        write_json(progress_path, state)
        print(json.dumps(state, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run all Gaia2 source-evidence repair batches with resumable checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from repair_gaia2_unresolved import load_env_file, run_v2
from review_gaia2_references import load_config


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
    args = parser.parse_args()

    load_env_file(args.env_file)
    config = load_config(args.config)
    config.concurrency = max(1, args.concurrency)
    inputs = sorted((args.out / "inputs").glob("*.json"))
    stop = args.stop if args.stop is not None else len(inputs)
    progress_path = args.out / "batch-driver-progress.json"
    for start in range(args.start, stop, args.batch_size):
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
        )
        state = {
            "batch_start": start,
            "batch_size": args.batch_size,
            "stop": stop,
            "summary": summary,
        }
        progress_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(state, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

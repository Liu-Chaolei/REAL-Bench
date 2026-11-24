#!/usr/bin/env python3
"""Validate repair ledger references and canonical Gaia2 task loading."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--repair-root", type=Path, required=True)
    args = parser.parse_args()
    tasks = json.loads((args.root / "tasks.json").read_text())
    task_ids = {task["id"] for task in tasks}
    variants_path = args.root / "interaction_variants.json"
    if variants_path.exists():
        task_ids.update(row["id"] for row in json.loads(variants_path.read_text()))
    variants_path = args.root / "interaction_variants.json"
    if variants_path.exists():
        task_ids.update(row["id"] for row in json.loads(variants_path.read_text()))
    ledger_path = args.repair_root / "repair-ledger.jsonl"
    if ledger_path.exists():
        for line in ledger_path.read_text().splitlines():
            entry = json.loads(line)
            if entry["task_id"] not in task_ids:
                raise ValueError(f"Unknown task in ledger: {entry['task_id']}")
            if entry["source_task_id"] not in task_ids:
                raise ValueError(f"Unknown source in ledger: {entry['source_task_id']}")
    print(json.dumps({"tasks": len(tasks), "ledger": ledger_path.exists()}))


if __name__ == "__main__":
    main()

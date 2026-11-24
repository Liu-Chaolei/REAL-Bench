#!/usr/bin/env python3
"""Validate the task/source invariants of a Gaia2 repair queue."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

SEVERITIES = {"info", "low", "medium", "high", "critical"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()

    inventory = json.loads((args.review_dir / "inventory.json").read_text())["rows"]
    expected = {row["id"]: row for row in inventory}
    rows = [json.loads(line) for line in args.queue.read_text().splitlines() if line]
    actual = {row["task_id"]: row for row in rows}
    if set(actual) != set(expected):
        raise ValueError(
            f"Queue/inventory mismatch: queue={len(actual)} inventory={len(expected)}"
        )

    for task_id, row in actual.items():
        source = expected[task_id]
        if row["kind"] != source["kind"]:
            raise ValueError(f"Kind mismatch for {task_id}")
        if row["source_task_id"] != source["source_task_id"]:
            raise ValueError(f"Source mismatch for {task_id}")
        if row["priority"] not in SEVERITIES:
            raise ValueError(f"Invalid priority for {task_id}")
        for finding in row["confirmed"] + row["needs_evidence"]:
            if finding["severity"] not in SEVERITIES:
                raise ValueError(f"Invalid finding severity for {task_id}")

    print(json.dumps({"tasks": len(rows), "kinds": Counter(r["kind"] for r in rows)}))


if __name__ == "__main__":
    main()

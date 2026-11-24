#!/usr/bin/env python3
"""Validate the task/source invariants of a Gaia2 repair queue."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

SEVERITIES = {"info", "low", "medium", "high", "critical"}


def task_digest(task):
    return hashlib.sha256(
        json.dumps(
            task, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args()

    inventory = json.loads((args.review_dir / "inventory.json").read_text())["rows"]
    expected = {row["id"]: row for row in inventory}
    rows = [json.loads(line) for line in args.queue.read_text().splitlines() if line]
    if len(rows) != 1200 or len({row.get("task_id") for row in rows}) != len(rows):
        raise ValueError(f"Queue must contain 1200 unique rows, got {len(rows)}")
    actual = {row["task_id"]: row for row in rows}
    if set(actual) != set(expected):
        raise ValueError(
            f"Queue/inventory mismatch: queue={len(actual)} inventory={len(expected)}"
        )

    tasks = {
        task["id"]: task
        for task in json.loads(
            (Path("data/tau2/domains/gaia2") / "tasks.json").read_text()
        )
    }
    for task_id, row in actual.items():
        source = expected[task_id]
        if row["kind"] != source["kind"]:
            raise ValueError(f"Kind mismatch for {task_id}")
        if row["source_task_id"] != source["source_task_id"]:
            raise ValueError(f"Source mismatch for {task_id}")
        if (
            row["priority"] not in SEVERITIES
            or row.get("issue_priority") not in SEVERITIES
        ):
            raise ValueError(f"Invalid priority for {task_id}")
        if row.get("evidence_priority") not in SEVERITIES:
            raise ValueError(f"Invalid evidence priority for {task_id}")
        source_task = tasks.get(row["source_task_id"])
        if source_task is None:
            raise ValueError(f"Missing source task for {task_id}")
        if row.get("source_sha256") != task_digest(source_task):
            raise ValueError(f"Source hash mismatch for {task_id}")
        for finding in row["confirmed"] + row["needs_evidence"]:
            if finding["severity"] not in SEVERITIES:
                raise ValueError(f"Invalid finding severity for {task_id}")
        if row.get("confirmed_issue_count") != len(row["confirmed"]):
            raise ValueError(f"Confirmed count mismatch for {task_id}")
        if row.get("needs_evidence_count") != len(row["needs_evidence"]):
            raise ValueError(f"Evidence count mismatch for {task_id}")

    print(json.dumps({"tasks": len(rows), "kinds": Counter(r["kind"] for r in rows)}))


if __name__ == "__main__":
    main()

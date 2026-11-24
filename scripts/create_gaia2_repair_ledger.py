#!/usr/bin/env python3
"""Create an auditable pending ledger from the validated Gaia2 repair queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.queue.read_text().splitlines() if line]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as stream:
        for row in rows:
            for status, findings in (("confirmed_issue", row["confirmed"]), ("needs_evidence", row["needs_evidence"])):
                for index, finding in enumerate(findings):
                    entry = {
                        "repair_id": f"{row['task_id']}:{status}:{index}",
                        "task_id": row["task_id"],
                        "source_task_id": row["source_task_id"],
                        "kind": row["kind"],
                        "stage": None,
                        "area": finding.get("normalized_area", "other"),
                        "severity": finding.get("severity", "info"),
                        "status": status,
                        "finding_title": finding.get("title", ""),
                        "old_value": None,
                        "new_value": None,
                        "reason": finding.get("impact", ""),
                        "evidence": finding.get("evidence", []),
                        "affected_variants": [],
                        "patch": None,
                        "validation": [],
                    }
                    stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps({"tasks": len(rows), "ledger": sum(1 for _ in args.out.open())}))


if __name__ == "__main__":
    main()

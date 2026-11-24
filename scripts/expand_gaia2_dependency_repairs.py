#!/usr/bin/env python3
"""Extract additional explicitly named dependency edges from review findings."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

PAIR_PATTERNS = (
    re.compile(r"from [`'\"]?([\w-]+)[`'\"]?\s+to [`'\"]?([\w-]+)", re.I),
    re.compile(r"([\w-]+)\s*->\s*([\w-]+)"),
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--base-plan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    tasks = {x["id"]: x for x in json.loads((args.data_root / "tasks.json").read_text())}
    existing = [json.loads(x) for x in args.base_plan.read_text().splitlines() if x]
    keys = {(x["task_id"], x["operation"]["event_id"], x["operation"].get("depends_on")) for x in existing}
    out = list(existing)
    counts = Counter()
    for line in args.queue.read_text().splitlines():
        row = json.loads(line)
        task = tasks.get(row["task_id"])
        if not task:
            continue
        events = {e["id"]: e for e in task["scenario"]["events"]}
        for n, finding in enumerate(row.get("confirmed", [])):
            rec = finding.get("recommendation", "")
            if "depend" not in rec.lower():
                continue
            for pidx, pattern in enumerate(PAIR_PATTERNS):
                for match in pattern.finditer(rec):
                    before, after = match.groups()
                    if before not in events or after not in events:
                        continue
                    key = (row["task_id"], after, before)
                    if key in keys or before in events[after].get("dependencies", []):
                        continue
                    keys.add(key)
                    out.append({
                        "repair_id": f"{row['task_id']}:expanded-dependency:{n}:{pidx}",
                        "task_id": row["task_id"],
                        "source_task_id": row["task_id"],
                        "status": "candidate",
                        "confidence": "high",
                        "reason": "Review recommendation explicitly names an ordered event pair.",
                        "evidence": finding.get("evidence", []),
                        "finding_title": finding.get("title", ""),
                        "operation": {"op": "add_dependency", "event_id": after, "depends_on": before},
                    })
                    counts["added"] += 1
    args.out.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n")
    print(json.dumps({"base": len(existing), "expanded": len(out)-len(existing), "total": len(out), "counts": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()

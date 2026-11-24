#!/usr/bin/env python3
"""Build a compact, task-deduplicated repair queue from offline review findings."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def area(f):
    s = (str(f.get("area", "")) + " " + str(f.get("title", ""))).lower()
    if any(
        x in s
        for x in ("fixed", "hardcod", "stale-id", "event_id", "index", "placeholder")
    ):
        return "reference_id_or_index"
    if any(x in s for x in ("stage", "dependency", "order", "sequence")):
        return "stage_dependency_order"
    if any(
        x in s
        for x in (
            "scoring",
            "criteria",
            "assertion",
            "reward",
            "check",
            "coverage",
            "over-constrain",
            "under-constrain",
        )
    ):
        return "scoring_coverage_or_overconstraint"
    if any(
        x in s
        for x in (
            "answer",
            "reply",
            "wrong",
            "result",
            "search",
            "evidence",
            "age",
            "count",
            "date",
        )
    ):
        return "answer_fact_or_evidence"
    if any(
        x in s
        for x in ("variant", "temporary", "follow-up", "cancel", "modify", "addtask")
    ):
        return "variant_semantics_or_coverage"
    return "other"


SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_ALIASES = {"moderate": "medium", "medium-high": "high", "warning": "low"}
VALID_AREAS = {
    "reference_id_or_index",
    "stage_dependency_order",
    "scoring_coverage_or_overconstraint",
    "answer_fact_or_evidence",
    "variant_semantics_or_coverage",
    "other",
}


def normalize_severity(value):
    raw = str(value or "").strip().lower().replace(" ", "_")
    normalized = SEVERITY_ALIASES.get(raw, raw)
    return normalized if normalized in SEVERITY_ORDER else "info"


def task_digest(task):
    """Return the same canonical task digest used by Gaia2 source bindings."""
    payload = json.dumps(
        task, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    d = json.loads((a.review_dir / "reference-summary.json").read_text())
    inventory = json.loads((a.review_dir / "inventory.json").read_text())["rows"]
    inventory_by_id = {row["id"]: row for row in inventory}
    data_root = Path("data/tau2/domains/gaia2")
    source_tasks = {
        row["id"]: row for row in json.loads((data_root / "tasks.json").read_text())
    }
    tasks = defaultdict(
        lambda: {
            "kind": None,
            "source_task_id": None,
            "confirmed": [],
            "needs_evidence": [],
            "areas": Counter(),
        }
    )
    for f in d["findings"]:
        if f["task_id"] not in inventory_by_id:
            raise ValueError(f"Finding references task outside inventory: {f['task_id']}")
        inventory_row = inventory_by_id[f["task_id"]]
        t = tasks[f["task_id"]]
        t["kind"] = inventory_row["kind"]
        t["source_task_id"] = inventory_row["source_task_id"]
        status = f.get("normalized_status")
        key = (
            "confirmed"
            if status == "confirmed_issue"
            else "needs_evidence"
            if status == "needs_evidence"
            else None
        )
        if key:
            row = {
                k: f.get(k)
                for k in (
                    "severity",
                    "area",
                    "title",
                    "evidence",
                    "impact",
                    "recommendation",
                    "related_event_ids",
                )
            }
            row["raw_status"] = f.get("status")
            row["normalized_status"] = status
            row["raw_severity"] = row["severity"]
            row["severity"] = normalize_severity(row["severity"])
            row["normalized_area"] = area(f)
            if row["normalized_area"] not in VALID_AREAS:
                raise ValueError(f"Unknown normalized area: {row['normalized_area']}")
            row["review_record"] = f.get("review_record")
            row["review_round"] = f.get("review_round", "reference")
            row["input_key"] = f.get("input_key")
            t[key].append(row)
            t["areas"][row["normalized_area"]] += 1
    rows = []
    for task, t in tasks.items():
        issue_levels = [x["severity"] for x in t["confirmed"]]
        evidence_levels = issue_levels + [x["severity"] for x in t["needs_evidence"]]
        issue_priority = max(issue_levels, key=SEVERITY_ORDER.get) if issue_levels else "info"
        evidence_priority = max(evidence_levels, key=SEVERITY_ORDER.get) if evidence_levels else "info"
        rows.append(
            {
                "task_id": task,
                "kind": t["kind"],
                "source_task_id": t["source_task_id"],
                "source_sha256": task_digest(
                    source_tasks[t["source_task_id"]]
                )
                if t["source_task_id"] in source_tasks
                else None,
                "priority": evidence_priority,
                "issue_priority": issue_priority,
                "evidence_priority": evidence_priority,
                "confirmed_issue_count": len(t["confirmed"]),
                "needs_evidence_count": len(t["needs_evidence"]),
                "areas": dict(t["areas"]),
                "confirmed": t["confirmed"],
                "needs_evidence": t["needs_evidence"],
                "repair_status": "pending",
            }
        )
    rows.sort(
        key=lambda r: (
            -SEVERITY_ORDER.get(r["priority"], 0),
            -r["confirmed_issue_count"],
            r["task_id"],
        )
    )
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    summary = Counter((r["kind"], r["priority"]) for r in rows)
    print(
        json.dumps(
            {
                "tasks": len(rows),
                "summary": {f"{k}:{p}": n for (k, p), n in summary.items()},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

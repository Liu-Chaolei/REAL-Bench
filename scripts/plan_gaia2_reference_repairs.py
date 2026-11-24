#!/usr/bin/env python3
"""Create conservative, auditable Gaia2 reference repair candidates.

The DeepSeek review is a triage source, not an authority for changing data.
This pass extracts only explicit dependency additions when both event IDs are
present in the task graph and the edge is currently absent.  All other
findings remain pending for evidence review.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

_ADD_EDGE = re.compile(
    r"add (?:a )?dependency(?: edge)? from [`'\"]?([^`'\"\s]+)[`'\"]?"
    r" to [`'\"]?([^`'\"\s.,)]+)",
    re.IGNORECASE,
)


def _task_map(root: Path) -> dict[str, dict]:
    tasks = json.loads((root / "tasks.json").read_text())
    result = {task["id"]: task for task in tasks}
    variants = root / "interaction_variants.json"
    if variants.exists():
        result.update({task["id"]: task for task in json.loads(variants.read_text())})
    return result


def _event_map(task: dict) -> dict[str, dict]:
    return {event["id"]: event for event in task.get("scenario", {}).get("events", [])}


def _candidate(task: dict, finding: dict, index: int) -> dict | None:
    recommendation = str(finding.get("recommendation", ""))
    match = _ADD_EDGE.search(recommendation)
    if not match:
        return None
    parent, child = match.groups()
    events = _event_map(task)
    if parent not in events or child not in events:
        return None
    if parent in events[child].get("dependencies", []):
        return None
    # Review recommendations use both ``from A to B`` conventions.  For the
    # small first pass, prefer the direction stated by the finding title and
    # evidence when they explicitly describe a later action (the action in
    # the title is the event receiving the dependency).
    title = str(finding.get("title", "")).lower()
    reverse = any(
        marker in title
        for marker in (
            "message is not ordered after",
            "final message' is not encoded",
            "after that save",
            "first cancellation is not dependent",
            "does not enforce tereza note before akira note",
            "message claiming the viewing is in the calendar",
        )
    )
    if reverse:
        parent, child = child, parent
    return {
        "repair_id": f"{task['id']}:dependency:{index}",
        "task_id": task["id"],
        "source_task_id": task["id"],
        "status": "candidate",
        "confidence": "high",
        "reason": "The review explicitly names both event IDs and requests a missing dependency edge.",
        "evidence": finding.get("evidence", []),
        "finding_title": finding.get("title", ""),
        "operation": {
            "op": "add_dependency",
            "event_id": child,
            "depends_on": parent,
        },
    }


def _recipient_candidate(task: dict, finding: dict, index: int) -> dict | None:
    text = " ".join(str(finding.get(key, "")) for key in ("title", "recommendation"))
    evidence = " ".join(str(value) for value in finding.get("evidence", []))
    if "recipient" not in text.lower() or "known email" not in text.lower():
        return None
    email_match = re.search(r"known_info\.email\s*=\s*['\"]([^'\"]+)", evidence)
    event_match = re.search(r"([\w-]+)\.arguments\.recipients\s*=", evidence)
    if not email_match or not event_match:
        return None
    events = _event_map(task)
    event_id = event_match.group(1)
    if event_id not in events:
        return None
    recipients = events[event_id].get("arguments", {}).get("recipients")
    if not isinstance(recipients, list) or email_match.group(1) in recipients:
        return None
    return {
        "repair_id": f"{task['id']}:recipient:{index}",
        "task_id": task["id"],
        "source_task_id": task["id"],
        "status": "candidate",
        "confidence": "high",
        "reason": "The review gives the user's known email and a conflicting recipient list.",
        "evidence": finding.get("evidence", []),
        "finding_title": finding.get("title", ""),
        "operation": {
            "op": "replace",
            "path": f"/scenario/events/{next(i for i, event in enumerate(task['scenario']['events']) if event['id'] == event_id)}/arguments/recipients",
            "event_id": event_id,
            "value": [email_match.group(1)],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    queue = [
        json.loads(line)
        for line in (
            args.review_dir.parent / "reference-repair-v1" / "repair-queue.jsonl"
        )
        .read_text()
        .splitlines()
        if line
    ]
    tasks = _task_map(args.data_root)
    candidates = []
    pending = Counter()
    for row in queue:
        task = tasks.get(row["task_id"])
        if task is None:
            pending["missing_task"] += 1
            continue
        for index, finding in enumerate(row.get("confirmed", [])):
            item = _candidate(task, finding, index)
            if item is None:
                item = _recipient_candidate(task, finding, index)
            if item is None:
                pending[finding.get("normalized_area", "other")] += 1
            else:
                candidates.append(item)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in candidates) + "\n"
    )
    summary = {
        "queue_tasks": len(queue),
        "candidate_repairs": len(candidates),
        "candidate_tasks": len({item["task_id"] for item in candidates}),
        "pending_confirmed_findings": sum(pending.values()),
        "pending_by_area": dict(pending),
        "policy": "explicit_missing_dependency_or_user_recipient_only",
    }
    (args.out.parent / "repair-plan-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

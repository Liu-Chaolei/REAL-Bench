#!/usr/bin/env python3
"""Apply the conservative dependency repair plan to an isolated artifact."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def has_cycle(task: dict) -> bool:
    events = {e["id"]: e for e in task.get("scenario", {}).get("events", [])}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(event_id: str) -> bool:
        if event_id in visiting:
            return True
        if event_id in visited:
            return False
        visiting.add(event_id)
        if any(
            parent not in events or visit(parent)
            for parent in events[event_id].get("dependencies", [])
        ):
            return True
        visiting.remove(event_id)
        visited.add(event_id)
        return False

    return any(visit(event_id) for event_id in events)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--review-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    args = parser.parse_args()

    tasks = json.loads((args.data_root / "tasks.json").read_text())
    by_id = {task["id"]: task for task in tasks}
    variants_path = args.data_root / "interaction_variants.json"
    variants = json.loads(variants_path.read_text()) if variants_path.exists() else []
    variant_sources = {row["id"]: row["source_task_id"] for row in variants}
    inventory = json.loads((args.review_dir / "inventory.json").read_text())["rows"]
    inventory_sources = {row["id"]: row["source_task_id"] for row in inventory}

    patched = copy.deepcopy(tasks)
    patched_by_id = {task["id"]: task for task in patched}
    applied = []
    skipped = []
    for line in args.plan.read_text().splitlines():
        if not line:
            continue
        item = json.loads(line)
        task_id = item["task_id"]
        source_id = inventory_sources.get(
            task_id, variant_sources.get(task_id, task_id)
        )
        task = patched_by_id.get(task_id)
        if task is None:
            skipped.append(
                {"repair_id": item["repair_id"], "reason": "source task missing"}
            )
            continue
        operation = item["operation"]
        events = {event["id"]: event for event in task["scenario"]["events"]}
        event = events.get(operation["event_id"])
        if event is None or (
            operation["op"] == "add_dependency"
            and operation["depends_on"] not in events
        ):
            skipped.append({"repair_id": item["repair_id"], "reason": "event missing"})
            continue
        if operation["op"] == "add_dependency":
            if operation["depends_on"] in event.setdefault("dependencies", []):
                skipped.append(
                    {"repair_id": item["repair_id"], "reason": "already present"}
                )
                continue
            old = list(event["dependencies"])
            event["dependencies"].append(operation["depends_on"])
            if has_cycle(task):
                event["dependencies"] = old
                skipped.append(
                    {"repair_id": item["repair_id"], "reason": "dependency cycle"}
                )
                continue
            new = list(event["dependencies"])
        elif operation["op"] == "replace" and operation.get("path", "").endswith(
            "/arguments/recipients"
        ):
            arguments = event.setdefault("arguments", {})
            old = list(arguments.get("recipients", []))
            new = list(operation["value"])
            if old == new:
                skipped.append(
                    {"repair_id": item["repair_id"], "reason": "already present"}
                )
                continue
            arguments["recipients"] = new
        else:
            skipped.append(
                {"repair_id": item["repair_id"], "reason": "unsupported operation"}
            )
            continue
        applied.append(
            {
                **item,
                "source_task_id": source_id,
                "status": "applied_to_isolated_artifact",
                "old_value": old,
                "new_value": new,
                "source_sha256_before": digest(by_id[source_id]),
            }
        )
        # Materialized repetition/mind-change rows are kept in the isolated
        # artifact too; apply the same source repair to the source row so a
        # later regeneration can propagate it deterministically.
        if source_id != task_id and source_id in patched_by_id:
            source_task = patched_by_id[source_id]
            source_events = {
                event["id"]: event for event in source_task["scenario"]["events"]
            }
            source_event = source_events.get(operation["event_id"])
            if source_event is not None:
                if operation["op"] == "add_dependency" and operation[
                    "depends_on"
                ] not in source_event.setdefault("dependencies", []):
                    source_event["dependencies"].append(operation["depends_on"])
                    if has_cycle(source_task):
                        source_event["dependencies"].pop()
                elif operation["op"] == "replace":
                    source_event.setdefault("arguments", {})["recipients"] = list(
                        operation["value"]
                    )

    args.out_root.mkdir(parents=True, exist_ok=True)
    (args.out_root / "tasks.json").write_text(
        json.dumps(patched, ensure_ascii=False, indent=2) + "\n"
    )
    (args.out_root / "repair-ledger-applied.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in applied) + "\n"
    )
    summary = {
        "source_tasks": len(tasks),
        "planned": sum(1 for line in args.plan.read_text().splitlines() if line),
        "applied": len(applied),
        "skipped": skipped,
        "patched_task_ids": sorted({item["source_task_id"] for item in applied}),
        "source_tasks_sha256_before": digest(tasks),
        "source_tasks_sha256_after": digest(patched),
    }
    (args.out_root / "repair-apply-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

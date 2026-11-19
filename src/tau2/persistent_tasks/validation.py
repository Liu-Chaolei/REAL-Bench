"""Cheap schema and source-binding checks; no model calls."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def validate_stage_payload(
    payload: dict[str, Any], source_events: set[str]
) -> list[str]:
    """Return structural errors for one native-task sidecar scenario."""
    errors: list[str] = []
    task = payload["task"]
    source_ids = set(task.get("source_event_ids", []))
    if not source_ids <= source_events:
        errors.append("source_event_ids contains an unknown source event")
    trigger_ids = {event["id"] for event in payload["triggers"].get("state_events", [])}
    for event in payload["triggers"].get("state_events", []):
        if not set(event.get("depends_on", [])) <= source_ids | trigger_ids:
            errors.append(f"unknown dependency in {event['id']}")
    for release in payload["triggers"].get("releases", []):
        if not set(release.get("depends_on", [])) <= source_ids | trigger_ids:
            errors.append(f"unknown release dependency in {release['request_id']}")
    return errors


def validate_sidecar(root: str | Path) -> list[str]:
    """Validate the complete draft sidecar against its native task graph."""
    root = Path(root)
    errors: list[str] = []

    def read(name):
        path = root / name
        if not path.exists():
            errors.append(f"missing sidecar file: {name}")
            return []
        value = json.loads(path.read_text())
        if not isinstance(value, list):
            errors.append(f"sidecar file is not a list: {name}")
            return []
        return value

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    tasks = read("tasks.json")
    triggers = {row.get("scenario_id"): row for row in read("triggers.json")}
    references = {row.get("scenario_id"): row for row in read("references.json")}
    scoring = {row.get("scenario_id"): row for row in read("scoring.json")}
    ids = [row.get("scenario_id") for row in tasks]
    if len(ids) != len(set(ids)):
        errors.append("duplicate scenario_id")
    if manifest.get("scenarios") != len(tasks):
        errors.append("manifest scenario count differs from tasks")
    if Counter(row.get("operation_type") for row in tasks) != Counter(
        {"association": 4, "query": 2, "modify": 2, "cancel": 1, "result": 1}
    ):
        errors.append("operation type quota differs from the draft manifest")

    native_tasks = {}
    tasks_path = root.parent / "tasks.json"
    if tasks_path.exists():
        native_tasks = {row["id"]: row for row in json.loads(tasks_path.read_text())}
    for row in tasks:
        scenario_id = row.get("scenario_id")
        source = native_tasks.get(row.get("source_task_id"))
        if source is None:
            errors.append(f"{scenario_id}: unknown source task")
            continue
        stages = source.get("scenario", {}).get("stages", [])
        stage = row.get("source_stage")
        if not isinstance(stage, int) or stage < 0 or stage >= len(stages):
            errors.append(f"{scenario_id}: invalid source stage")
            continue
        source_ids = set(row.get("source_event_ids", []))
        stage_ids = set(stages[stage].get("event_ids", []))
        if not source_ids or not source_ids <= stage_ids:
            errors.append(f"{scenario_id}: source event outside selected stage")
        request_ids = {request.get("request_id") for request in row.get("requests", [])}
        if not request_ids or len(request_ids) != len(row.get("requests", [])):
            errors.append(f"{scenario_id}: invalid request IDs")
        for name, table in (
            ("triggers", triggers),
            ("references", references),
            ("scoring", scoring),
        ):
            if scenario_id not in table:
                errors.append(f"{scenario_id}: missing {name} record")
        trigger = triggers.get(scenario_id, {})
        state_ids = {event.get("id") for event in trigger.get("state_events", [])}
        allowed = source_ids | state_ids
        for event in trigger.get("state_events", []):
            if not set(event.get("depends_on", [])) <= allowed:
                errors.append(f"{scenario_id}: invalid state-event dependency")
        for release in trigger.get("releases", []):
            if release.get("request_id") not in request_ids:
                errors.append(f"{scenario_id}: release references unknown request")
            if not set(release.get("depends_on", [])) <= allowed:
                errors.append(f"{scenario_id}: invalid release dependency")
        for operation in references.get(scenario_id, {}).get("operations", []):
            target = operation.get("target", {})
            if (
                target.get("source_task_id") != row.get("source_task_id")
                or target.get("source_stage") != stage
            ):
                errors.append(
                    f"{scenario_id}: reference target differs from task identity"
                )
    return errors

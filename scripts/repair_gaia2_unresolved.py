#!/usr/bin/env python3
"""Second-pass Gaia2 unresolved-finding review and repair.

This is an offline authoring tool. It builds compact, finding-focused inputs,
asks one model request per task for decisions and JSON Patch operations, and
applies accepted operations to an isolated v2 artifact.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from repair_gaia2_references import (
    DirectModel,
    allowed_path,
    apply_operation,
    load_env_file,
    load_sources,
    read_json,
    sha,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tau2" / "domains" / "gaia2"
REVIEW = ROOT / "data" / "archives" / "gaia2" / "reference-review-v2"
V1 = ROOT / "data" / "archives" / "gaia2" / "reference-repair-simple-v1"
DEFAULT_OUT = ROOT / "data" / "archives" / "gaia2" / "reference-repair-simple-v2"
VERSION = "gaia2-reference-repair-unresolved-v2"

REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_index": {"type": "integer"},
                    "decision": {
                        "type": "string",
                        "enum": ["fix", "keep", "unresolved"],
                    },
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["finding_index", "decision", "reason", "evidence"],
                "additionalProperties": False,
            },
        },
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["add", "remove", "replace"],
                    },
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["op", "path"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["decisions", "operations", "summary"],
    "additionalProperties": False,
}


def size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def task_inventory(review_dir: Path) -> dict[str, dict]:
    return {row["id"]: row for row in read_json(review_dir / "inventory.json")["rows"]}


def load_v1(v1_root: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    task_path = v1_root / "repaired-tasks.json"
    variant_path = v1_root / "repaired-interaction-variants.json"
    if task_path.exists() and variant_path.exists():
        return (
            {row["id"]: row for row in read_json(task_path)},
            {row["id"]: row for row in read_json(variant_path)},
        )
    return load_sources(DATA)


def unresolved_rows(
    *, v1_root: Path, review_dir: Path, data_root: Path
) -> list[dict[str, Any]]:
    inventory = task_inventory(review_dir)
    tasks, variants = load_v1(v1_root)
    rows = []
    response_dir = v1_root / "responses"
    for response_path in sorted(response_dir.glob("*.json")):
        response = read_json(response_path)
        indexes = response.get("unresolved_findings")
        if response.get("status") != "complete" or not isinstance(indexes, list):
            continue
        task_id = response.get("task_id") or response_path.stem
        inventory_row = inventory.get(task_id)
        if inventory_row is None:
            continue
        review_path = review_dir / "responses" / "reference" / f"{task_id}.json"
        if not review_path.exists():
            continue
        review_record = read_json(review_path)
        findings = (review_record.get("output") or {}).get("findings", [])
        selected = []
        for value in indexes:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(findings):
                selected.append({"index": index, **findings[index - 1]})
        if not selected:
            continue
        if inventory_row["source_task_id"] not in tasks:
            continue
        rows.append(
            {
                "task_id": task_id,
                "kind": inventory_row["kind"],
                "source_task_id": inventory_row["source_task_id"],
                "target_type": (
                    "interaction_variant" if task_id in variants else "task"
                ),
                "findings": selected,
                "current_target": copy.deepcopy(
                    variants.get(task_id, tasks.get(task_id))
                ),
                "source_target": copy.deepcopy(tasks[inventory_row["source_task_id"]]),
            }
        )
    return rows


def finding_text(findings: list[dict[str, Any]]) -> str:
    return json.dumps(findings, ensure_ascii=False)


def matching_event_ids(events: list[dict], findings: list[dict]) -> set[str]:
    encoded = finding_text(findings)
    event_ids = {event.get("id") for event in events}
    selected = {event_id for event_id in event_ids if event_id and event_id in encoded}
    return selected


def compact_events(
    task: dict[str, Any], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    scenario = task.get("scenario") or {}
    events = scenario.get("events") or []
    by_id = {event.get("id"): event for event in events}
    selected_ids = matching_event_ids(events, findings)
    # Include one dependency hop so ordering can be judged without sending the graph.
    for event_id in list(selected_ids):
        event = by_id.get(event_id, {})
        selected_ids.update(event.get("dependencies") or [])
    if not selected_ids:
        # Keep a small index when the finding has no explicit event reference.
        return [
            {
                "path": f"/scenario/events/{index}",
                "id": event.get("id"),
                "kind": event.get("kind"),
                "tool": event.get("tool"),
                "dependencies": event.get("dependencies", []),
            }
            for index, event in enumerate(events)
        ]
    result = []
    for index, event in enumerate(events):
        if event.get("id") not in selected_ids:
            continue
        result.append(
            {
                "path": f"/scenario/events/{index}",
                "id": event.get("id"),
                "kind": event.get("kind"),
                "tool": event.get("tool"),
                "arguments": copy.deepcopy(event.get("arguments", {})),
                "dependencies": copy.deepcopy(event.get("dependencies", [])),
            }
        )
    return result


def stage_numbers(findings: list[dict[str, Any]], stage_count: int) -> set[int]:
    encoded = finding_text(findings).lower()
    result = {
        int(value)
        for value in re.findall(r"(?:stage|stages)[ _-]*(\d+)", encoded)
        if int(value) < stage_count
    }
    for value in re.findall(r"/stages/(\d+)", encoded):
        if int(value) < stage_count:
            result.add(int(value))
    return result


def compact_stages(
    task: dict[str, Any], findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    stages = (task.get("scenario") or {}).get("stages") or []
    selected_numbers = stage_numbers(findings, len(stages))
    if not selected_numbers:
        selected_numbers = set(range(len(stages)))
    encoded = finding_text(findings).lower()
    explicit_check_indexes = {
        int(value) for value in re.findall(r"/checks/(\d+)", encoded)
    }
    check_issue = any(
        token in encoded
        for token in ("check", "criteria", "scoring", "reward", "assertion")
    )
    selected_event_ids = matching_event_ids(
        (task.get("scenario") or {}).get("events") or [], findings
    )
    relevant_checks = []
    relevant_criteria: dict[str, Any] = {}
    result = []
    for index, stage in enumerate(stages):
        if index not in selected_numbers:
            continue
        result.append(
            {
                "path": f"/scenario/stages/{index}",
                "stage": stage.get("stage", index),
                "request": stage.get("request"),
                "event_ids": stage.get("event_ids", []),
                "relation": stage.get("relation"),
                "preconditions": stage.get("preconditions", []),
            }
        )
        checks = stage.get("checks") or []
        for check_index, check in enumerate(checks):
            path = f"/scenario/stages/{index}/checks/{check_index}"
            reference_event_id = (check.get("reference") or {}).get("event_id")
            keep_check = check_index in explicit_check_indexes
            keep_check = keep_check or reference_event_id in selected_event_ids
            if keep_check:
                relevant_checks.append(
                    {
                        "path": path,
                        "stage": index,
                        "check": copy.deepcopy(check),
                    }
                )
            elif check_issue and check_index < 8:
                # Preserve a small index for generic scoring findings without
                # copying a potentially huge expected/reference payload.
                relevant_checks.append(
                    {
                        "path": path,
                        "stage": index,
                        "check": {
                            "operation": check.get("operation"),
                            "expected": str(check.get("expected", ""))[:500],
                            "reference": check.get("reference"),
                        },
                    }
                )
        criteria = stage.get("criteria") or {}
        keys = []
        if "communicate" in encoded:
            keys.append("communicate_info")
        if "nl_assert" in encoded:
            keys.append("nl_assertions")
        if "env_assert" in encoded or "postcondition" in encoded:
            keys.append("env_assertions")
        if "reward" in encoded:
            keys.append("reward_basis")
        if "criteria" in encoded and not keys:
            keys = ["communicate_info", "env_assertions", "reward_basis"]
        for key in keys:
            if key in criteria:
                relevant_criteria[f"/scenario/stages/{index}/criteria/{key}"] = (
                    copy.deepcopy(criteria[key])
                )
    return result, relevant_checks, relevant_criteria


def snapshot_evidence(
    task: dict[str, Any], findings: list[dict[str, Any]], data_root: Path
) -> list[dict[str, Any]]:
    snapshot_id = (task.get("scenario") or {}).get("snapshot")
    if not snapshot_id:
        return []
    path = data_root / "snapshots" / f"{snapshot_id}.json.gz"
    if not path.exists():
        return []
    encoded = finding_text(findings)
    tokens = {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_@+.-]{4,}|[\u4e00-\u9fff]{2,}", encoded)
    }
    if not tokens:
        return []
    try:
        with gzip.open(path, "rt") as stream:
            snapshot = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return []
    records: list[dict[str, Any]] = []
    flat_matches: list[dict[str, Any]] = []

    def visit(node: Any, pointer: str = "") -> None:
        if len(records) >= 30 and len(flat_matches) >= 120:
            return
        if isinstance(node, dict):
            encoded_node = json.dumps(node, ensure_ascii=False).casefold()
            if (
                len(records) < 30
                and any(token in encoded_node for token in tokens)
                and len(encoded_node) <= 18000
            ):
                records.append({"path": pointer, "record": node})
            for key, child in node.items():
                visit(child, f"{pointer}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{pointer}/{index}")
        elif isinstance(node, (str, int, float, bool)):
            if len(flat_matches) < 120 and any(
                token in str(node).casefold() for token in tokens
            ):
                flat_matches.append({"path": pointer, "value": node})

    visit(snapshot)
    result = []
    if records:
        result.append({"type": "records", "items": records})
    if flat_matches:
        result.append({"type": "matching_fields", "items": flat_matches})
    encoded_result = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded_result) > 50000:
        result = [{"type": "matching_fields", "items": flat_matches[:60]}]
    return result


def current_reference_answer(task: dict[str, Any], variant: dict | None) -> Any:
    if variant is not None and variant.get("reference_answer"):
        return variant["reference_answer"]
    answers = []
    for event in (task.get("scenario") or {}).get("events", []):
        if event.get("kind") == "reference" and event.get("channel") == "agent":
            answers.append(event.get("arguments", {}).get("content"))
    return answers


def user_request(task: dict[str, Any], variant: dict | None) -> str:
    if variant is not None:
        return "\n".join(
            value
            for value in (
                variant.get("initial_request"),
                variant.get("followup_request"),
                variant.get("temporary_request"),
            )
            if value
        )
    instructions = (task.get("user_scenario") or {}).get("instructions") or {}
    if isinstance(instructions, str):
        return instructions
    return "\n".join(
        value
        for value in (
            instructions.get("reason_for_call"),
            instructions.get("task_instructions"),
        )
        if value
    )


def compact_source_context(
    source: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    variant = None
    events = compact_events(source, findings)
    stages, checks, criteria = compact_stages(source, findings)
    return {
        "user_request": user_request(source, variant),
        "relevant_events": events,
        "relevant_stages": stages,
        "relevant_checks": checks,
        "relevant_criteria": criteria,
    }


def build_input(row: dict[str, Any], data_root: Path) -> dict[str, Any]:
    target = row["current_target"]
    variant = target if row["target_type"] == "interaction_variant" else None
    stages, checks, criteria = compact_stages(target, row["findings"])
    package = {
        "version": VERSION,
        "task_id": row["task_id"],
        "kind": row["kind"],
        "source_task_id": row["source_task_id"],
        "target_type": row["target_type"],
        "user_request": user_request(target, variant),
        "current_target": copy.deepcopy(target),
        "current_reference_answer": current_reference_answer(target, variant),
        "relevant_events": compact_events(target, row["findings"]),
        "relevant_stages": stages,
        "relevant_checks": checks,
        "relevant_criteria": criteria,
        "unresolved_findings": copy.deepcopy(row["findings"]),
        "evidence": snapshot_evidence(target, row["findings"], data_root),
    }
    if row["target_type"] == "interaction_variant":
        package["source_context"] = compact_source_context(
            row["source_target"], row["findings"]
        )
    return package


def repair_prompt_v2(package: dict[str, Any]) -> str:
    target_type = package["target_type"]
    prompt_package = prompt_package_v2(package)
    return f"""You are performing a second offline review and repair of one Gaia2 reference task.

The task has unresolved findings from a previous review. Decide every finding as fix, keep, or unresolved, and apply all confirmed fixes in the same response. Use only the supplied task wording and evidence. Do not guess facts. Preserve correct content. If evidence is insufficient, use unresolved and do not create an operation.

The target remains a complete {target_type} JSON object locally. JSON Patch paths must point into that complete object, even though the prompt contains only relevant excerpts. Do not modify user request text, IDs, source IDs, snapshot IDs, or variant user-request fields. For variants, modify only reference_events or reference_answer.

Return JSON only with decisions and operations. A fix decision must have a corresponding operation. A keep decision must not change the target. Use unresolved when the current content should be retained because evidence is insufficient.

PACKAGE:
{json.dumps(prompt_package, ensure_ascii=False, separators=(",", ":"))}
"""


def prompt_package_v2(package: dict[str, Any]) -> dict[str, Any]:
    """Bound the model view while keeping the local full target for patching."""
    value = copy.deepcopy(
        {key: item for key, item in package.items() if key != "current_target"}
    )
    finding_blob = json.dumps(
        value.get("unresolved_findings", []), ensure_ascii=False
    )
    referenced_ids = {
        event.get("id")
        for event in value.get("relevant_events", [])
        if event.get("id") and event["id"] in finding_blob
    }
    events = value.get("relevant_events", [])
    if size(events) > 16000:
        reduced = []
        for event in events:
            if event.get("id") in referenced_ids:
                reduced.append(event)
            else:
                reduced.append(
                    {
                        key: event.get(key)
                        for key in ("path", "id", "kind", "tool", "dependencies")
                        if key in event
                    }
                )
        value["relevant_events"] = reduced
    if size(value.get("relevant_events", [])) > 18000:
        for event in value["relevant_events"]:
            arguments = event.get("arguments")
            if isinstance(arguments, dict):
                event["arguments"] = {
                    key: (
                        child[:600] + "...[truncated]"
                        if isinstance(child, str) and len(child) > 600
                        else child
                    )
                    for key, child in arguments.items()
                }
    checks = value.get("relevant_checks", [])
    if size(checks) > 12000:
        value["relevant_checks"] = [
            {
                "path": item.get("path"),
                "stage": item.get("stage"),
                "check": {
                    key: (
                        str(item.get("check", {}).get(key, ""))[:500]
                        if key == "expected"
                        else item.get("check", {}).get(key)
                    )
                    for key in ("operation", "expected", "reference", "compare_to")
                    if key in item.get("check", {})
                },
            }
            for item in checks
        ]
    evidence = value.get("evidence", [])
    if size(evidence) > 12000:
        bounded = []
        for item in evidence:
            if not isinstance(item, dict):
                bounded.append(item)
                continue
            clipped = copy.deepcopy(item)
            for child in clipped.get("items", []):
                if isinstance(child, dict) and isinstance(child.get("record"), dict):
                    child["record"] = {
                        key: (
                            str(val)[:500] + "...[truncated]"
                            if isinstance(val, str) and len(val) > 500
                            else val
                        )
                        for key, val in child["record"].items()
                    }
            if isinstance(clipped.get("items"), list):
                clipped["items"] = clipped["items"][:40]
            bounded.append(clipped)
        value["evidence"] = bounded
    findings = value.get("unresolved_findings", [])
    if size(findings) > 16000:
        compact_findings = []
        for finding in findings:
            compact_findings.append(
                {
                    key: (
                        [
                            item[:900] + "...[truncated]"
                            if isinstance(item, str) and len(item) > 900
                            else item
                            for item in finding.get("evidence", [])[:8]
                        ]
                        if key == "evidence"
                        else (
                            str(finding.get(key, ""))[:1500] + "...[truncated]"
                            if key == "recommendation"
                            and len(str(finding.get(key, ""))) > 1500
                            else finding.get(key)
                        )
                    )
                    for key in (
                        "index",
                        "status",
                        "severity",
                        "area",
                        "title",
                        "evidence",
                        "recommendation",
                        "related_event_ids",
                    )
                    if key in finding
                }
            )
        value["unresolved_findings"] = compact_findings
    source_context = value.get("source_context")
    if isinstance(source_context, dict):
        source_events = source_context.get("relevant_events", [])
        if size(source_events) > 16000:
            source_context["relevant_events"] = [
                {
                    key: event.get(key)
                    for key in ("path", "id", "kind", "tool", "dependencies")
                    if key in event
                }
                for event in source_events
            ]
        source_checks = source_context.get("relevant_checks", [])
        if size(source_checks) > 12000:
            source_context["relevant_checks"] = [
                {
                    "path": item.get("path"),
                    "stage": item.get("stage"),
                    "check": {
                        key: str(item.get("check", {}).get(key, ""))[:500]
                        if key == "expected"
                        else item.get("check", {}).get(key)
                        for key in ("operation", "expected", "reference", "compare_to")
                        if key in item.get("check", {})
                    },
                }
                for item in source_checks
            ]
    return value


def task_has_cycle(task: dict[str, Any]) -> bool:
    events = {
        event.get("id"): event
        for event in (task.get("scenario") or {}).get("events", [])
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(event_id: str) -> bool:
        if event_id in visiting:
            return True
        if event_id in visited:
            return False
        visiting.add(event_id)
        for dependency in events.get(event_id, {}).get("dependencies", []):
            if dependency not in events or visit(dependency):
                return True
        visiting.remove(event_id)
        visited.add(event_id)
        return False

    return any(visit(event_id) for event_id in events)


def validate_one(
    target: dict[str, Any], target_type: str, operations: list[dict]
) -> list[str]:
    errors = []
    for event in (target.get("scenario") or {}).get("events", []):
        event_id = event.get("id")
        if not event_id:
            errors.append("event without id")
        for dependency in event.get("dependencies", []):
            if dependency not in {
                item.get("id")
                for item in (target.get("scenario") or {}).get("events", [])
            }:
                errors.append(f"missing dependency: {dependency}")
    if target_type == "task" and task_has_cycle(target):
        errors.append("dependency cycle")
    try:
        if target_type == "task":
            from tau2.data_model.tasks import Task

            Task.model_validate(target)
        else:
            from tau2.domains.gaia2.interaction_variants import InteractionVariant

            InteractionVariant.model_validate(target)
    except ValidationError as exc:
        errors.append(str(exc.errors()[0]))
    return errors


def prepare_v2(
    *, v1_root: Path, review_dir: Path, data_root: Path, out: Path, limit: int | None
) -> dict[str, Any]:
    rows = unresolved_rows(v1_root=v1_root, review_dir=review_dir, data_root=data_root)
    if limit is not None:
        rows = rows[:limit]
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for row in rows:
        package = build_input(row, data_root)
        write_json(out / "inputs" / f"{row['task_id']}.json", package)
        records.append(
            {
                "task_id": row["task_id"],
                "kind": row["kind"],
                "source_task_id": row["source_task_id"],
                "finding_indexes": [item["index"] for item in row["findings"]],
                "input_chars": len(repair_prompt_v2(package)),
            }
        )
    (out / "unresolved-input.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n"
    )
    summary = {
        "version": VERSION,
        "tasks": len(rows),
        "input_chars_median": (
            int(sorted(item["input_chars"] for item in records)[len(records) // 2])
            if records
            else 0
        ),
        "review_dir": str(review_dir),
        "data_root": str(data_root),
        "v1_root": str(v1_root),
    }
    write_json(out / "prepare-summary.json", summary)
    return summary


def run_v2(
    *,
    v1_root: Path,
    review_dir: Path,
    data_root: Path,
    out: Path,
    config,
    limit: int | None,
    offline: bool,
) -> dict[str, Any]:
    task_map, variant_map = load_v1(v1_root)
    packages = [read_json(path) for path in sorted((out / "inputs").glob("*.json"))]
    if limit is not None:
        packages = packages[:limit]
    model = DirectModel(config, out / "requests")
    responses_dir = out / "responses"
    tasks = copy.deepcopy(task_map)
    variants = copy.deepcopy(variant_map)
    changes = []
    unresolved = []

    def one(package):
        key = sha(
            {
                "version": VERSION,
                "task_id": package["task_id"],
                "current_target": package["current_target"],
                "findings": package["unresolved_findings"],
                "relevant_events": package["relevant_events"],
                "relevant_stages": package["relevant_stages"],
                "evidence": package["evidence"],
            }
        )
        response_path = responses_dir / f"{package['task_id']}.json"
        if response_path.exists():
            old = read_json(response_path)
            if offline or (
                old.get("input_key") == key and old.get("status") == "complete"
            ):
                return package, old
        if offline:
            return package, {
                "task_id": package["task_id"],
                "status": "error",
                "detail": "no cached response",
                "input_key": key,
            }
        try:
            raw = model.complete(repair_prompt_v2(package), REPAIR_SCHEMA)
            response = json.loads(raw)
            response.update(
                task_id=package["task_id"], input_key=key, status="complete"
            )
        except Exception as exc:
            response = {
                "task_id": package["task_id"],
                "input_key": key,
                "status": "error",
                "error": type(exc).__name__,
                "detail": str(exc),
            }
        write_json(response_path, response)
        return package, response

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        results = list(pool.map(one, packages))

    for package, response in results:
        task_id = package["task_id"]
        target = copy.deepcopy(package["current_target"])
        operations = (
            response.get("operations", [])
            if response.get("status") == "complete"
            else []
        )
        errors = []
        valid_operations = []
        for operation in operations:
            if not isinstance(operation, dict) or not operation.get("op"):
                errors.append("invalid operation")
                continue
            path = operation.get("path", "")
            if package["target_type"] == "task" and path.startswith(
                ("/events/", "/stages/")
            ):
                operation = dict(operation)
                operation["path"] = "/scenario" + path
            if not allowed_path(package["target_type"], operation.get("path", "")):
                errors.append(f"forbidden path: {operation.get('path')}")
                continue
            try:
                apply_operation(target, operation)
                valid_operations.append(operation)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append(str(exc))
        if response.get("status") == "complete":
            errors.extend(
                validate_one(target, package["target_type"], valid_operations)
            )
        if errors:
            target = copy.deepcopy(package["current_target"])
        decisions = response.get("decisions", [])
        unresolved_indexes = [
            item.get("finding_index", item.get("index"))
            for item in decisions
            if item.get("decision") == "unresolved"
        ]
        if response.get("status") != "complete":
            unresolved_indexes = [
                item["index"] for item in package["unresolved_findings"]
            ]
        if package["target_type"] == "task":
            tasks[task_id] = target
        else:
            variants[task_id] = target
        if errors:
            change_status = "failed"
        elif valid_operations:
            change_status = "fixed"
        elif unresolved_indexes:
            change_status = "unresolved"
        else:
            change_status = "kept"
        changes.append(
            {
                "task_id": task_id,
                "status": change_status,
                "decisions": decisions,
                "changed_paths": [item["path"] for item in valid_operations]
                if not errors
                else [],
                "before_sha256": sha(package["current_target"]),
                "after_sha256": sha(target),
                "validation": {"errors": errors, "ok": not errors},
            }
        )
        for index in unresolved_indexes:
            unresolved.append(
                {
                    "task_id": task_id,
                    "finding_index": index,
                    "reason": next(
                        (
                            item.get("reason", "")
                            for item in decisions
                            if item.get("finding_index", item.get("index")) == index
                        ),
                        response.get("detail", "Evidence remains insufficient"),
                    ),
                    "current_content_kept": True,
                }
            )

    for variant in variants.values():
        source = tasks.get(variant["source_task_id"])
        if source is not None:
            variant["source_sha256"] = sha(source)

    write_json(out / "repaired-tasks.json", list(tasks.values()))
    write_json(out / "repaired-interaction-variants.json", list(variants.values()))
    runtime_tasks = list(tasks.values())
    materialization_errors = []
    try:
        from tau2.data_model.tasks import Task
        from tau2.domains.gaia2.interaction_variants import (
            InteractionVariant,
            apply_variant,
        )

        for raw_variant in variants.values():
            try:
                source = Task.model_validate(tasks[raw_variant["source_task_id"]])
                variant = InteractionVariant.model_validate(raw_variant)
                runtime_tasks.append(
                    apply_variant(source, variant).model_dump(mode="json")
                )
            except Exception as exc:
                materialization_errors.append(
                    {"task_id": raw_variant.get("id"), "error": str(exc)}
                )
    except Exception as exc:
        materialization_errors.append({"scope": "runtime_import", "error": str(exc)})
    write_json(out / "repaired-all-tasks.json", runtime_tasks)
    (out / "changes.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in changes) + "\n"
    )
    (out / "unresolved.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in unresolved) + "\n"
    )
    summary = {
        "version": VERSION,
        "tasks": len(packages),
        "fixed": sum(item["status"] == "fixed" for item in changes),
        "unresolved": sum(item["status"] == "unresolved" for item in changes),
        "failed": sum(item["status"] == "failed" for item in changes),
        "unresolved_finding_records": len(unresolved),
        "source_tasks": len(tasks),
        "interaction_variants": len(variants),
        "runtime_tasks": len(runtime_tasks),
        "materialization_errors": materialization_errors,
    }
    write_json(out / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("unresolved-prepare", "unresolved-repair"))
    parser.add_argument("--input-root", type=Path, default=V1)
    parser.add_argument("--review-dir", type=Path, default=REVIEW)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    if args.command == "unresolved-prepare":
        print(
            json.dumps(
                prepare_v2(
                    v1_root=args.input_root,
                    review_dir=args.review_dir,
                    data_root=args.data_root,
                    out=args.out,
                    limit=args.limit,
                ),
                ensure_ascii=False,
            )
        )
    else:
        if not (args.out / "inputs").exists():
            prepare_v2(
                v1_root=args.input_root,
                review_dir=args.review_dir,
                data_root=args.data_root,
                out=args.out,
                limit=args.limit,
            )
        from review_gaia2_references import load_config

        config = load_config(args.config)
        if args.concurrency is not None:
            config.concurrency = max(1, args.concurrency)

        print(
            json.dumps(
                run_v2(
                    v1_root=args.input_root,
                    review_dir=args.review_dir,
                    data_root=args.data_root,
                    out=args.out,
                    config=config,
                    limit=args.limit,
                    offline=args.offline,
                ),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

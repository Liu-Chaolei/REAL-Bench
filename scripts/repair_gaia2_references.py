#!/usr/bin/env python3
"""Apply the saved Gaia2 review results to an isolated repair artifact.

The repair pass is intentionally small: one model request per task returns
JSON-Patch-like operations.  The original task files are never edited.  A
successful request is cached per task, so a run can be resumed after an API
failure without repeating completed work.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from review_gaia2_references import (
    ReviewConfig,
    load_config,
    read_json,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tau2" / "domains" / "gaia2"
REVIEW = ROOT / "data" / "archives" / "gaia2" / "reference-review-v2"
DEFAULT_OUT = ROOT / "data" / "archives" / "gaia2" / "reference-repair-simple-v1"
REPAIR_VERSION = "gaia2-reference-repair-simple-v1"

REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["changed", "unchanged", "unresolved"]},
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["add", "remove", "replace"]},
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["op", "path"],
                "additionalProperties": False,
            },
        },
        "unresolved_findings": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["status", "summary", "rationale", "operations", "unresolved_findings"],
    "additionalProperties": False,
}


def sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def inventory_rows(review_dir: Path) -> list[dict[str, Any]]:
    return read_json(review_dir / "inventory.json")["rows"]


def load_sources(root: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    tasks = {row["id"]: row for row in read_json(root / "tasks.json")}
    variants_path = root / "interaction_variants.json"
    variants = (
        {row["id"]: row for row in read_json(variants_path)}
        if variants_path.exists()
        else {}
    )
    return tasks, variants


def load_env_file(path: Path) -> None:
    """Load non-secret environment settings without printing their values."""
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        try:
            parsed = shlex.split(value, comments=True)
        except ValueError:
            continue
        os.environ[key] = parsed[0] if parsed else ""


def compact_task(task: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields a repair needs while bounding very large task payloads."""
    scenario = task.get("scenario") or {}
    stages = []
    for stage in scenario.get("stages", []):
        stages.append(
            {
                key: copy.deepcopy(stage.get(key))
                for key in (
                    "stage",
                    "request",
                    "event_ids",
                    "relation",
                    "preconditions",
                    "checks",
                    "criteria",
                )
            }
        )
    result = {
        "id": task.get("id"),
        "description": task.get("description"),
        "user_scenario": task.get("user_scenario"),
        "evaluation_criteria": task.get("evaluation_criteria"),
        "scenario": {
            key: copy.deepcopy(scenario.get(key))
            for key in (
                "version",
                "start_time",
                "timezone",
                "source_id",
                "source_sha256",
                "snapshot",
                "category",
                "events",
                "runtime_sha256",
                "assets_sha256",
            )
        },
    }
    result["scenario"]["stages"] = stages
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 190_000:
        return result
    # Evidence is available through the snapshot and the saved review input.
    for stage in result["scenario"]["stages"]:
        stage.pop("preconditions", None)
        stage.pop("checks", None)
        stage["criteria"] = {
            key: stage.get("criteria", {}).get(key)
            for key in ("env_assertions", "communicate_info")
            if key in (stage.get("criteria") or {})
        }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 190_000:
        return result

    # Preserve all event IDs and arguments, but bound unusually verbose prose.
    def trim(value):
        if isinstance(value, dict):
            return {key: trim(child) for key, child in value.items()}
        if isinstance(value, list):
            return [trim(child) for child in value]
        if isinstance(value, str) and len(value) > 1800:
            return value[:1800] + "...[truncated for repair prompt]"
        return value

    result = trim(result)
    return result


def review_for(review_dir: Path, task_id: str) -> dict[str, Any]:
    path = review_dir / "responses" / "reference" / f"{task_id}.json"
    if not path.exists():
        return {"status": "missing", "findings": []}
    row = read_json(path)
    output = row.get("output") if row.get("status") == "complete" else None
    return output or {"status": "missing", "findings": []}


class DirectModel:
    """Small OpenAI-compatible caller used when the full tau2 package is unavailable."""

    def __init__(self, config: ReviewConfig, cache_dir: Path):
        self.config = config
        self.cache_dir = cache_dir
        self.lock = threading.Lock()
        self.used = 0

    def complete(self, prompt: str, schema: dict[str, Any]) -> str:
        from litellm import completion

        identity = sha(
            {
                "prompt": prompt,
                "schema": schema,
                "model": self.config.model,
                "repair_protocol": "v2-proxy-no-output-limit",
            }
        )
        path = self.cache_dir / f"{identity}.json"
        if path.exists():
            cached = read_json(path)
            if cached.get("identity") == identity and isinstance(
                cached.get("output"), str
            ):
                return cached["output"]
        with self.lock:
            if self.used >= self.config.max_requests:
                raise RuntimeError("Repair request budget exhausted")
            self.used += 1
        arguments = dict(self.config.args)
        arguments.setdefault("timeout", self.config.timeout_seconds)
        # The configured DeepSeek proxy exposes an OpenAI-compatible endpoint;
        # LiteLLM rejects the generic reasoning_effort argument for this route.
        arguments.pop("reasoning_effort", None)
        if self.config.api_base:
            arguments["api_base"] = self.config.api_base
        if self.config.api_key_env:
            api_key = os.environ.get(self.config.api_key_env)
            if not api_key:
                raise ValueError(
                    f"Missing repair credential: {self.config.api_key_env}"
                )
            arguments["api_key"] = api_key
        response = completion(
            model=self.config.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            **arguments,
        )
        content = response.choices[0].message.content or ""
        write_json(path, {"identity": identity, "output": content})
        return content


def prepare_inputs(
    *, review_dir: Path, data_root: Path, out: Path, limit: int | None = None
) -> int:
    tasks, variants = load_sources(data_root)
    rows = inventory_rows(review_dir)
    if limit is not None:
        rows = rows[:limit]
    for row in rows:
        task_id = row["id"]
        if row["kind"] in {"base", "repetition", "mind_change"}:
            target = tasks[task_id]
            target_type = "task"
        else:
            target = variants[task_id]
            target_type = "interaction_variant"
        original = {
            "task_id": task_id,
            "kind": row["kind"],
            "source_task_id": row["source_task_id"],
            "target_type": target_type,
            "target": target,
            "target_for_prompt": compact_task(target)
            if target_type == "task"
            else target,
            "source_task": compact_task(tasks[row["source_task_id"]])
            if target_type == "interaction_variant"
            else None,
            "review": review_for(review_dir, task_id),
        }
        write_json(out / "inputs" / f"{task_id}.json", original)
    write_json(
        out / "prepare-summary.json",
        {
            "version": REPAIR_VERSION,
            "prepared": len(rows),
            "review_dir": str(review_dir),
            "data_root": str(data_root),
            "source_tasks_sha256": sha(read_json(data_root / "tasks.json")),
            "source_variants_sha256": sha(
                read_json(data_root / "interaction_variants.json")
            )
            if (data_root / "interaction_variants.json").exists()
            else None,
        },
    )
    return len(rows)


def repair_prompt(material: dict[str, Any]) -> str:
    target_type = material["target_type"]
    target_text = json.dumps(
        material["target_for_prompt"], ensure_ascii=False, separators=(",", ":")
    )
    review_text = json.dumps(
        material["review"], ensure_ascii=False, separators=(",", ":")
    )
    source_text = ""
    if material.get("source_task") is not None:
        source_text = (
            "\nSOURCE TASK (read-only context; do not modify it in this response):\n"
            + json.dumps(
                material["source_task"], ensure_ascii=False, separators=(",", ":")
            )
        )
    target_rules = (
        "The target is one complete Task JSON object. Return JSON operations whose paths are relative to that object."
        if target_type == "task"
        else "The target is one InteractionVariant JSON object. Modify only reference_events or reference_answer; do not rewrite user request fields."
    )
    return f"""You are repairing one Gaia2 benchmark reference answer using a saved offline review.

{target_rules}
The review is a set of findings, not an authority. Check each finding against the target and source context. Apply only changes supported by the task wording, the review evidence, or the supplied source data. Preserve all correct content. Never invent a contact, address, date, amount, identifier, result, or user requirement. Do not modify user_scenario, request text, IDs, source IDs, snapshot IDs, or variant user-request fields. If evidence is insufficient, leave the target unchanged and list the finding number in unresolved_findings.

Operations use JSON Pointer paths (for example /scenario/events/2/dependencies/0). Use replace for an existing value, add for a new object/list value, and remove only when the review clearly shows the value is unsupported. Return complete JSON only. For an unchanged target use an empty operations list.

TASK ID: {material["task_id"]}
TARGET JSON:
{target_text}
{source_text}
SAVED DEEPSEEK REVIEW:
{review_text}
"""


def allowed_path(target_type: str, path: str) -> bool:
    if not path.startswith("/"):
        return False
    forbidden = (
        "/id",
        "/user_scenario",
        "/scenario/source_id",
        "/scenario/source_sha256",
        "/scenario/snapshot",
    )
    if any(path == item or path.startswith(item + "/") for item in forbidden):
        return False
    if target_type == "interaction_variant":
        return path.startswith("/reference_events") or path.startswith(
            "/reference_answer"
        )
    return True


def pointer_tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def apply_operation(document: Any, operation: dict[str, Any]) -> Any:
    tokens = pointer_tokens(operation["path"])
    if not tokens:
        raise ValueError("Root replacement is not supported")
    parent = document
    for token in tokens[:-1]:
        if isinstance(parent, list):
            parent = parent[int(token)]
        elif isinstance(parent, dict) and token in parent:
            parent = parent[token]
        else:
            raise ValueError(f"Missing JSON Pointer parent: {operation['path']}")
    leaf = tokens[-1]
    op = operation["op"]
    if isinstance(parent, list):
        if op == "add" and leaf == "-":
            parent.append(copy.deepcopy(operation.get("value")))
        else:
            index = int(leaf)
            if op == "add":
                parent.insert(index, copy.deepcopy(operation.get("value")))
            elif op == "replace":
                parent[index] = copy.deepcopy(operation.get("value"))
            elif op == "remove":
                parent.pop(index)
            else:
                raise ValueError(f"Unsupported operation: {op}")
    elif isinstance(parent, dict):
        if op in {"add", "replace"}:
            if op == "replace" and leaf not in parent:
                raise ValueError(f"Cannot replace missing key: {operation['path']}")
            parent[leaf] = copy.deepcopy(operation.get("value"))
        elif op == "remove":
            if leaf not in parent:
                raise ValueError(f"Cannot remove missing key: {operation['path']}")
            del parent[leaf]
        else:
            raise ValueError(f"Unsupported operation: {op}")
    else:
        raise ValueError(f"JSON Pointer parent is not mutable: {operation['path']}")
    return document


def validate_operations(
    material: dict[str, Any], response: dict[str, Any]
) -> tuple[dict, list[str]]:
    target = copy.deepcopy(material["target"])
    errors = []
    operations = response.get("operations", [])
    if response.get("status") == "unchanged" and operations:
        errors.append("unchanged response contains operations")
    valid_operations = []
    for operation in operations:
        if not isinstance(operation, dict) or not operation.get("op"):
            errors.append("operation is not a JSON object with an op")
            continue
        operation = dict(operation)
        # The task prompt shows a complete Task object, but models occasionally
        # omit the top-level ``scenario`` prefix when addressing stage fields.
        # Resolve that harmless shorthand before applying the operation.
        path = operation.get("path", "")
        if material["target_type"] == "task" and path.startswith(
            ("/stages/", "/events/")
        ):
            operation["path"] = "/scenario" + path
        if not allowed_path(material["target_type"], operation.get("path", "")):
            errors.append(f"forbidden path: {operation.get('path')}")
            continue
        try:
            apply_operation(target, operation)
            valid_operations.append(operation)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append(str(exc))
    if material["target_type"] == "task":
        try:
            from tau2.data_model.tasks import Task

            Task.model_validate(target)
        except ValidationError as exc:
            errors.append(f"repaired task does not validate: {exc.errors()[0]}")
    else:
        try:
            from tau2.domains.gaia2.interaction_variants import InteractionVariant

            InteractionVariant.model_validate(target)
        except ValidationError as exc:
            errors.append(f"repaired variant does not validate: {exc.errors()[0]}")
    # Keep valid operations even when another model operation was malformed;
    # the malformed operation is reported as unresolved below.
    response["operations"] = valid_operations
    return target, errors


def load_rule_operations(
    plan: Path | None, inventory: list[dict[str, Any]], tasks: dict[str, dict]
) -> dict[str, list[dict[str, Any]]]:
    """Load the small set of explicit, high-confidence repairs already extracted."""
    if plan is None or not plan.exists():
        return {}
    inventory_by_id = {row["id"]: row for row in inventory}
    by_source: dict[str, list[dict[str, Any]]] = {}
    for line in plan.read_text().splitlines():
        if not line.strip():
            continue
        candidate = json.loads(line)
        row = inventory_by_id.get(candidate.get("task_id"))
        source_id = row["source_task_id"] if row else candidate.get("source_task_id")
        if source_id not in tasks:
            continue
        operation = candidate.get("operation", {})
        if operation.get("op") == "add_dependency":
            events = tasks[source_id].get("scenario", {}).get("events", [])
            event_index = next(
                (
                    index
                    for index, event in enumerate(events)
                    if event.get("id") == operation.get("event_id")
                ),
                None,
            )
            if event_index is None:
                continue
            operation = {
                "op": "add",
                "path": f"/scenario/events/{event_index}/dependencies/-",
                "value": operation.get("depends_on"),
            }
        elif operation.get("op") == "replace":
            # Existing candidate paths are already task-relative.
            operation = {
                key: value
                for key, value in operation.items()
                if key in {"op", "path", "value"}
            }
        else:
            continue
        by_source.setdefault(source_id, []).append(
            {
                "operation": operation,
                "repair_id": candidate.get("repair_id"),
                "finding_title": candidate.get("finding_title", ""),
            }
        )
    return by_source


def run_repairs(
    *,
    review_dir: Path,
    data_root: Path,
    out: Path,
    config: ReviewConfig,
    limit: int | None = None,
    dry_run: bool = False,
    rule_plan: Path | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    rows = inventory_rows(review_dir)
    if limit is not None:
        rows = rows[:limit]
    model_client = None if dry_run else DirectModel(config, out / "requests")

    def repair_one(row):
        path = out / "inputs" / f"{row['id']}.json"
        if not path.exists():
            raise FileNotFoundError(path)
        material = read_json(path)
        key = f"{REPAIR_VERSION}:{row['id']}:{sha(material['target'])}:{sha(material['review'])}"
        response_path = out / "responses" / f"{row['id']}.json"
        if response_path.exists():
            old = read_json(response_path)
            if offline:
                return material, old
            reusable_statuses = {"complete"} | ({"dry_run"} if dry_run else set())
            if old.get("input_key") == key and old.get("status") in reusable_statuses:
                return material, old
        if dry_run:
            response = {
                "task_id": row["id"],
                "kind": row["kind"],
                "input_key": key,
                "status": "dry_run",
                "model_status": "planned",
                "operations": [],
                "unresolved_findings": [],
            }
        else:
            try:
                raw = model_client.complete(repair_prompt(material), REPAIR_SCHEMA)
                response = json.loads(raw)
                response.update(
                    task_id=row["id"],
                    kind=row["kind"],
                    input_key=key,
                    status="complete",
                )
            except Exception as exc:
                response = {
                    "task_id": row["id"],
                    "kind": row["kind"],
                    "input_key": key,
                    "status": "error",
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
        write_json(response_path, response)
        return material, response

    results = []
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        futures = [pool.submit(repair_one, row) for row in rows]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda pair: pair[0]["task_id"])

    tasks, variants = load_sources(data_root)
    rule_operations = load_rule_operations(rule_plan, rows, tasks)
    repaired_tasks = copy.deepcopy(tasks)
    repaired_variants = copy.deepcopy(variants)
    changes = []
    unresolved = []
    for material, response in results:
        task_id = material["task_id"]
        model_operations = []
        if response.get("status") == "complete":
            repaired, errors = validate_operations(material, response)
            model_operations = response.get("operations", [])
        else:
            repaired, errors = material["target"], []
        deterministic_operations = []
        if material["target_type"] == "task":
            for candidate in rule_operations.get(task_id, []):
                operation = candidate["operation"]
                if not allowed_path("task", operation.get("path", "")):
                    continue
                try:
                    before = copy.deepcopy(repaired)
                    apply_operation(repaired, operation)
                    # A candidate can be repeated safely after a resumed run.
                    deterministic_operations.append(operation)
                except (ValueError, KeyError, IndexError, TypeError):
                    repaired = before
        operations = model_operations + deterministic_operations
        if response.get("status") == "error" and not deterministic_operations:
            errors.append(response.get("detail", "model error"))
        hard_errors = [
            error
            for error in errors
            if str(error).startswith("repaired task does not validate")
            or str(error).startswith("repaired variant does not validate")
        ]
        if hard_errors or (response.get("status") == "error" and not operations):
            if response.get("status") == "error" and not operations:
                if material["target_type"] == "interaction_variant":
                    original_variant = copy.deepcopy(variants[task_id])
                    source = repaired_tasks.get(original_variant["source_task_id"])
                    if source is not None:
                        original_variant["source_sha256"] = sha(source)
                    repaired_variants[task_id] = original_variant
                changes.append(
                    {
                        "task_id": task_id,
                        "status": "unresolved",
                        "changed_paths": [],
                        "reason": "Repair request failed; original content retained.",
                        "errors": errors,
                    }
                )
                unresolved.append(
                    {
                        "task_id": task_id,
                        "reason": "Repair request failed; original content retained.",
                        "errors": errors,
                    }
                )
                continue
            if material["target_type"] == "interaction_variant":
                # Keep the original variant delta when a model response cannot
                # be represented by the strict InteractionVariant schema.
                original_variant = copy.deepcopy(variants[task_id])
                source = repaired_tasks.get(original_variant["source_task_id"])
                if source is not None:
                    original_variant["source_sha256"] = sha(source)
                repaired_variants[task_id] = original_variant
                changes.append(
                    {
                        "task_id": task_id,
                        "status": "unresolved",
                        "changed_paths": [],
                        "reason": "Variant repair was rejected; original variant retained.",
                        "errors": hard_errors or errors,
                    }
                )
                unresolved.append(
                    {"task_id": task_id, "reason": "Invalid variant repair; original retained."}
                )
                continue
            if response.get("status") == "error":
                unresolved.append(
                    {
                        "task_id": task_id,
                        "reason": response.get("detail", "model error"),
                    }
                )
            changes.append(
                {
                    "task_id": task_id,
                    "status": "failed",
                    "changed_paths": [],
                    "errors": errors,
                }
            )
            continue
        if material["target_type"] == "task":
            repaired_tasks[task_id] = repaired
        else:
            repaired_variants[task_id] = repaired
        status = (
            "changed"
            if operations
            else (
                "unresolved"
                if response.get("unresolved_findings") or errors
                else "unchanged"
            )
        )
        changes.append(
            {
                "task_id": task_id,
                "status": status,
                "changed_paths": [op["path"] for op in operations],
                "reason": response.get("summary", "")
                or (
                    "Applied explicit repair candidate from the saved review."
                    if deterministic_operations
                    else "No supported repair was applied."
                ),
                "rationale": response.get("rationale", ""),
                "source_findings": response.get("unresolved_findings", [])
                + [item.get("repair_id") for item in rule_operations.get(task_id, [])],
                "before_sha256": sha(material["target"]),
                "after_sha256": sha(repaired),
            }
        )
        if errors:
            unresolved.append(
                {
                    "task_id": task_id,
                    "reason": "Some model operations were invalid and were skipped.",
                    "errors": errors,
                }
            )
        if response.get("unresolved_findings"):
            unresolved.append(
                {
                    "task_id": task_id,
                    "finding_indexes": response["unresolved_findings"],
                    "reason": response.get(
                        "rationale", "Evidence remains insufficient"
                    ),
                }
            )
        continue

    # A changed base task changes the pinned source revision of its interaction variants.
    for variant in repaired_variants.values():
        source = repaired_tasks.get(variant["source_task_id"])
        if source is not None:
            variant["source_sha256"] = sha(source)

    # A malformed variant repair must not poison the complete 1200-task
    # artifact. Keep that variant's original delta and report it as unresolved.
    from tau2.data_model.tasks import Task
    from tau2.domains.gaia2.interaction_variants import (
        InteractionVariant,
        apply_variant,
    )

    changes_by_id = {row["task_id"]: row for row in changes}
    for variant_id, variant in list(repaired_variants.items()):
        try:
            source = Task.model_validate(repaired_tasks[variant["source_task_id"]])
            materialized = apply_variant(source, InteractionVariant.model_validate(variant))
            event_ids = {event.id for event in materialized.scenario.events}
            missing = sorted(
                {
                    dependency
                    for event in materialized.scenario.events
                    for dependency in event.dependencies
                    if dependency not in event_ids
                }
            )
            if missing:
                raise ValueError(f"missing dependency {missing[0]}")
        except Exception as exc:
            original = copy.deepcopy(variants[variant_id])
            original["source_sha256"] = sha(repaired_tasks[original["source_task_id"]])
            repaired_variants[variant_id] = original
            row = changes_by_id.get(variant_id)
            if row is not None:
                row["status"] = "unresolved"
                row["errors"] = [f"variant repair reverted: {exc}"]
            unresolved.append(
                {"task_id": variant_id, "reason": "Variant repair reverted", "error": str(exc)}
            )

    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "repaired-tasks.json", list(repaired_tasks.values()))
    write_json(
        out / "repaired-interaction-variants.json", list(repaired_variants.values())
    )
    # Materialize the same 1200-task runtime view used by the Gaia2 loader.
    # Keeping this file makes the repair result directly inspectable without
    # changing the formal source/variant data contract.
    runtime_tasks = list(repaired_tasks.values())
    for raw_variant in repaired_variants.values():
        source = Task.model_validate(repaired_tasks[raw_variant["source_task_id"]])
        variant = InteractionVariant.model_validate(raw_variant)
        runtime_tasks.append(apply_variant(source, variant).model_dump(mode="json"))
    write_json(out / "repaired-all-tasks.json", runtime_tasks)
    (out / "changes.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in changes) + "\n"
    )
    (out / "unresolved.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in unresolved) + "\n"
    )
    summary = {
        "version": REPAIR_VERSION,
        "tasks_selected": len(rows),
        "changed": sum(row["status"] == "changed" for row in changes),
        "unchanged": sum(row["status"] == "unchanged" for row in changes),
        "unresolved": sum(row["status"] == "unresolved" for row in changes),
        "failed": sum(row["status"] == "failed" for row in changes),
        "source_tasks": len(repaired_tasks),
        "interaction_variants": len(repaired_variants),
        "runtime_tasks": len(runtime_tasks),
        "source_tasks_sha256": sha(list(repaired_tasks.values())),
        "source_variants_sha256": sha(list(repaired_variants.values())),
        "review_dir": str(review_dir),
        "data_root": str(data_root),
    }
    write_json(out / "manifest.json", summary)
    return summary


def validate_artifact(out: Path, review_dir: Path, data_root: Path) -> dict[str, Any]:
    from tau2.data_model.tasks import Task
    from tau2.domains.gaia2.interaction_variants import (
        InteractionVariant,
        apply_variant,
    )

    tasks = read_json(out / "repaired-tasks.json")
    variants = read_json(out / "repaired-interaction-variants.json")
    errors = []
    task_by_id = {}
    for raw in tasks:
        if raw.get("id") in task_by_id:
            errors.append(f"duplicate task id: {raw.get('id')}")
        try:
            task_by_id[raw["id"]] = Task.model_validate(raw)
        except ValidationError as exc:
            errors.append(f"task {raw.get('id')} invalid: {exc.errors()[0]}")
    variant_by_id = {}
    for raw in variants:
        try:
            variant_by_id[raw["id"]] = InteractionVariant.model_validate(raw)
        except ValidationError as exc:
            errors.append(f"variant {raw.get('id')} invalid: {exc.errors()[0]}")
    if len(tasks) != 1000 or len(variants) != 200:
        errors.append(
            f"expected 1000 tasks and 200 variants, got {len(tasks)} and {len(variants)}"
        )
    runtime_count = 0
    for variant in variant_by_id.values():
        source = task_by_id.get(variant.source_task_id)
        if source is None:
            errors.append(f"missing source for variant {variant.id}")
            continue
        try:
            runtime = apply_variant(source, variant)
            runtime_count += 1
            events = {event.id: event for event in runtime.scenario.events}
            for event in events.values():
                for dependency in event.dependencies:
                    if dependency not in events:
                        errors.append(f"{runtime.id}: missing dependency {dependency}")
        except Exception as exc:
            errors.append(f"variant {variant.id} cannot materialize: {exc}")
    if len(tasks) + len(variants) != 1200:
        errors.append("repair artifact does not cover 1200 records")
    result = {
        "version": REPAIR_VERSION,
        "ok": not errors,
        "tasks": len(tasks),
        "variants": len(variants),
        "runtime_variants": runtime_count,
        "errors": errors,
        "review_inventory": len(inventory_rows(review_dir)),
        "source_data": str(data_root),
    }
    write_json(out / "validation-report.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "repair", "validate"))
    parser.add_argument("--review-dir", type=Path, default=REVIEW)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rule-plan", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Apply cached responses without issuing model requests.",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        load_env_file(args.env_file)
    if args.command == "prepare":
        print(
            json.dumps(
                {
                    "prepared": prepare_inputs(
                        review_dir=args.review_dir,
                        data_root=args.data_root,
                        out=args.out,
                        limit=args.limit,
                    )
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "repair":
        if not (args.out / "inputs").exists():
            prepare_inputs(
                review_dir=args.review_dir,
                data_root=args.data_root,
                out=args.out,
                limit=args.limit,
            )
        summary = run_repairs(
            review_dir=args.review_dir,
            data_root=args.data_root,
            out=args.out,
            config=load_config(args.config),
            limit=args.limit,
            dry_run=args.dry_run,
            rule_plan=args.rule_plan or (args.out / "rule-based-plan.jsonl"),
            offline=args.offline,
        )
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(
            json.dumps(
                validate_artifact(args.out, args.review_dir, args.data_root),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

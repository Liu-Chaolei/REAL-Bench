"""Per-task checkpoints and cumulative exports for offline Gaia2 repairs."""

from __future__ import annotations

import copy
import fcntl
import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from repair_gaia2_references import pointer_tokens, read_json, sha, write_json


@contextmanager
def exclusive_output(out: Path):
    """Avoid concurrent batch writers; OS releases the lock after a crash."""
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".repair.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another repair batch owns this output directory"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def apply_patch(target: dict, operations: list[dict]) -> dict:
    """Apply a whole patch strictly; caller only receives a successful copy."""
    result = copy.deepcopy(target)
    for operation in operations:
        if operation.get("op") not in {"add", "replace", "remove"}:
            raise ValueError("Unsupported patch operation")
        if operation["op"] != "remove" and "value" not in operation:
            raise ValueError("Patch value missing")
        tokens = pointer_tokens(operation["path"])
        parent = result
        for token in tokens[:-1]:
            if isinstance(parent, list):
                if not token.isdigit() or int(token) >= len(parent):
                    raise ValueError("Invalid array index")
                parent = parent[int(token)]
            else:
                parent = parent[token]
        leaf = tokens[-1]
        op = operation["op"]
        if isinstance(parent, list):
            if leaf == "-" and op == "add":
                parent.append(copy.deepcopy(operation["value"]))
                continue
            if not leaf.isdigit():
                raise ValueError("Invalid array index")
            index = int(leaf)
            if index > len(parent) or (op != "add" and index == len(parent)):
                raise ValueError("Array index out of bounds")
            if op == "add":
                parent.insert(index, copy.deepcopy(operation["value"]))
            elif op == "remove":
                parent.pop(index)
            else:
                parent[index] = copy.deepcopy(operation["value"])
        else:
            if op != "add" and leaf not in parent:
                raise ValueError("Patch target missing")
            if op == "remove":
                del parent[leaf]
            else:
                parent[leaf] = copy.deepcopy(operation["value"])
    return result


def graph_errors(task: dict) -> set[str]:
    """Check only local identity, stage membership and dependency validity."""
    events = (task.get("scenario") or {}).get("events", [])
    ids = [event["id"] for event in events]
    errors = set()
    if len(set(ids)) != len(ids):
        errors.add("duplicate event IDs")
    by_id = {event["id"]: event for event in events}
    for event in events:
        for parent in event.get("dependencies", []):
            if parent not in by_id:
                errors.add(f"missing dependency: {event['id']} -> {parent}")
    for stage in (task.get("scenario") or {}).get("stages", []):
        for event_id in stage.get("event_ids", []):
            if event_id not in by_id:
                errors.add(f"unknown stage event: {event_id}")
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            errors.add(f"dependency cycle: {key}")
            return
        if key in visited or key not in by_id:
            return
        visiting.add(key)
        for parent in by_id[key].get("dependencies", []):
            visit(parent)
        visiting.remove(key)
        visited.add(key)

    for key in by_id:
        visit(key)
    return errors


def locked_content(target: dict, target_type: str) -> dict:
    """Protect original requests even against replacement of parent objects."""
    if target_type == "interaction_variant":
        return {
            key: value
            for key, value in target.items()
            if key not in {"reference_events", "reference_answer", "source_sha256"}
        }
    spec = target.get("scenario") or {}
    return {
        "id": target["id"],
        "user_scenario": target.get("user_scenario"),
        "source_id": spec.get("source_id"),
        "source_sha256": spec.get("source_sha256"),
        "snapshot": spec.get("snapshot"),
        "requests": [stage["request"] for stage in spec.get("stages", [])],
        "user_events": [
            event for event in spec.get("events", []) if event["kind"] == "user"
        ],
    }


def check_candidate(before: dict, target: dict, target_type: str, tasks: dict) -> dict:
    """Reject a local patch without treating pre-existing defects as new ones."""
    from tau2.data_model.tasks import Task
    from tau2.domains.gaia2.interaction_variants import (
        InteractionVariant,
        apply_variant,
    )

    if locked_content(before, target_type) != locked_content(target, target_type):
        raise ValueError("Patch changes protected identity or user request")
    if target_type == "task":
        Task.model_validate(target)
        errors = graph_errors(target) - graph_errors(before)
    else:
        source = Task.model_validate(tasks[target["source_task_id"]])
        variant = copy.deepcopy(target)
        variant["source_sha256"] = sha(source.model_dump(mode="json"))
        view = apply_variant(source, InteractionVariant.model_validate(variant))
        original = dict(before, source_sha256=variant["source_sha256"])
        old_view = apply_variant(source, InteractionVariant.model_validate(original))
        errors = graph_errors(view.model_dump(mode="json")) - graph_errors(
            old_view.model_dump(mode="json")
        )
    if errors:
        raise ValueError("; ".join(sorted(errors)))
    return {"task_model": True, "dependencies": True, "protected_requests": True}


def record_result(package: dict, response: dict, tasks: dict, key: str) -> dict:
    """Decide, validate and checkpoint one atomic repair; errors keep all findings open."""
    before = package["current_target"]
    expected = {item["index"] for item in package["unresolved_findings"]}
    decisions, errors = {}, []
    for item in response.get("decisions", []):
        try:
            index = int(item.get("finding_index", item.get("index")))
            decision = item.get("decision", item.get("status"))
            if (
                index not in expected
                or index in decisions
                or decision not in {"fix", "keep", "unresolved"}
            ):
                raise ValueError("Unknown, duplicate or invalid finding decision")
            decisions[index] = {
                "finding_index": index,
                "decision": decision,
                "reason": item.get("reason", item.get("rationale", "")),
                "evidence": item.get("evidence", []),
            }
        except (TypeError, ValueError, AttributeError) as exc:
            errors.append(str(exc))
    for index in expected - decisions.keys():
        decisions[index] = {
            "finding_index": index,
            "decision": "unresolved",
            "reason": response.get("detail", "No decision returned"),
            "evidence": [],
        }
    operations = (
        response.get("operations", []) if response.get("status") == "complete" else []
    )
    target, validation = before, {}
    try:
        if response.get("status") != "complete":
            raise ValueError(response.get("detail", "Request incomplete"))
        if errors:
            raise ValueError("; ".join(errors))
        if operations and not any(
            row["decision"] == "fix" for row in decisions.values()
        ):
            raise ValueError("Patch has no fix decision")
        if not operations and any(
            row["decision"] == "fix" for row in decisions.values()
        ):
            raise ValueError("Fix decision has no patch")
        target = apply_patch(before, operations)
        validation = check_candidate(before, target, package["target_type"], tasks)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        target = before
        for row in decisions.values():
            if row["decision"] == "fix":
                row.update(decision="unresolved", reason="Patch rejected: " + str(exc))
    unresolved = [row for row in decisions.values() if row["decision"] == "unresolved"]
    return {
        "task_id": package["task_id"],
        "target_type": package["target_type"],
        "input_key": key,
        "status": "failed"
        if errors
        else (
            "fixed"
            if sha(target) != sha(before)
            else "unresolved"
            if unresolved
            else "kept"
        ),
        "decisions": list(decisions.values()),
        "operations": operations if not errors else [],
        "changed_paths": [op["path"] for op in operations] if not errors else [],
        "before_sha256": sha(before),
        "after_sha256": sha(target),
        "target": target,
        "validation": {**validation, "ok": not errors, "errors": errors},
        "source_evidence_sha256": sha(package.get("source_evidence")),
        "unresolved_count": len(unresolved),
    }


def export_results(out: Path, baseline_tasks: dict, baseline_variants: dict) -> dict:
    """Rebuild cumulative artifacts from immutable baseline plus task checkpoints.

    No patches are applied twice. Unprocessed tasks stay at the baseline. This
    is serialization/materialization, not a final evaluator or replay run.
    """
    from tau2.data_model.tasks import Task
    from tau2.domains.gaia2.interaction_variants import (
        InteractionVariant,
        apply_variant,
    )

    tasks, variants = copy.deepcopy(baseline_tasks), copy.deepcopy(baseline_variants)
    records = [read_json(path) for path in sorted((out / "results").glob("*.json"))]
    for row in records:
        original = (
            baseline_tasks if row["target_type"] == "task" else baseline_variants
        )[row["task_id"]]
        if sha(original) != row["before_sha256"]:
            raise ValueError(f"Baseline changed for {row['task_id']}")
        if sha(row["target"]) != row["after_sha256"]:
            raise ValueError(f"Checkpoint digest mismatch for {row['task_id']}")
        (tasks if row["target_type"] == "task" else variants)[row["task_id"]] = row[
            "target"
        ]
    runtime = list(tasks.values())
    for variant in variants.values():
        source = Task.model_validate(tasks[variant["source_task_id"]])
        variant["source_sha256"] = sha(source.model_dump(mode="json"))
        view = apply_variant(source, InteractionVariant.model_validate(variant))
        runtime.append(view.model_dump(mode="json"))
    write_json(out / "repaired-tasks.json", list(tasks.values()))
    write_json(out / "repaired-interaction-variants.json", list(variants.values()))
    write_json(out / "repaired-all-tasks.json", runtime)
    changes, unresolved = [], []
    for row in records:
        changes.append({key: value for key, value in row.items() if key != "target"})
        for decision in row["decisions"]:
            if decision["decision"] == "unresolved":
                unresolved.append({"task_id": row["task_id"], **decision})
    for name, rows in (("changes", changes), ("unresolved", unresolved)):
        path = out / f"{name}.jsonl"
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        )
        temporary.replace(path)
    summary = {
        "version": "source-evidence-batches-1",
        "processed": len(records),
        "statuses": dict(Counter(row["status"] for row in records)),
        "evidence_reviews": dict(
            Counter(row.get("evidence_review", "unknown") for row in records)
        ),
        "tasks_with_open_findings": sum(
            bool(row["unresolved_count"]) for row in records
        ),
        "unresolved_findings": len(unresolved),
        "source_tasks": len(tasks),
        "interaction_variants": len(variants),
        "runtime_tasks": len(runtime),
    }
    write_json(out / "summary.json", summary)
    write_json(
        out / "progress.json",
        {**summary, "completed_ids": [row["task_id"] for row in records]},
    )
    return summary

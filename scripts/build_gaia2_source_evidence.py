#!/usr/bin/env python3
"""Build source-grounded evidence packets for unresolved Gaia2 findings.

This tool never calls a model and never edits task data. It resolves the
original snapshot/stage evidence locally so a later repair request can rely on
verified source values instead of review prose alone.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tau2" / "domains" / "gaia2"
REVIEW = ROOT / "data" / "archives" / "gaia2" / "reference-review-v2"
V2 = ROOT / "data" / "archives" / "gaia2" / "reference-repair-simple-v2"
DEFAULT_OUT = ROOT / "data" / "archives" / "gaia2" / "reference-repair-simple-v3"
VERSION = "gaia2-source-evidence-v3"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def pointer_tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer: {pointer}")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    ]


def pointer_get(value: Any, pointer: str) -> Any:
    current = value
    for token in pointer_tokens(pointer):
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


def equal_value(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    if isinstance(actual, str) and isinstance(expected, str):
        return " ".join(actual.split()) == " ".join(expected.split())
    return str(actual) == str(expected)


def load_snapshot(snapshot_id: str, data_root: Path) -> list[dict[str, Any]]:
    path = data_root / "snapshots" / f"{snapshot_id}.json.gz"
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


def app_states(snapshot: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        app["name"]: app.get("app_state", {})
        for app in snapshot
        if app.get("name")
    }


def resolve_snapshot_pointer(
    snapshot: list[dict[str, Any]], pointer: str
) -> tuple[Any, str | None]:
    states = app_states(snapshot)
    try:
        return pointer_get(states, pointer), None
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "pointer_not_found"


def extract_pointers(text: str) -> list[str]:
    # Review evidence usually embeds RFC-6901 paths in prose.
    candidates = re.findall(r"(?<![A-Za-z0-9_])(/[A-Za-z0-9_./~:@+()\-]+)", text)
    result = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:'\")")
        if candidate not in result and len(candidate) > 2:
            result.append(candidate)
    return result


def stage_numbers(text: str, count: int) -> set[int]:
    values = {
        int(value)
        for value in re.findall(r"(?:stage|stages)[ _-]*(\d+)", text.lower())
        if int(value) < count
    }
    for value in re.findall(r"/stages/(\d+)", text):
        if int(value) < count:
            values.add(int(value))
    return values


def inventory(review_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: row
        for row in read_json(review_dir / "inventory.json")["rows"]
    }


def load_current_targets(v2_root: Path, data_root: Path) -> tuple[dict, dict]:
    task_path = v2_root / "repaired-tasks.json"
    variant_path = v2_root / "repaired-interaction-variants.json"
    if task_path.exists() and variant_path.exists():
        return (
            {row["id"]: row for row in read_json(task_path)},
            {row["id"]: row for row in read_json(variant_path)},
        )
    return (
        {row["id"]: row for row in read_json(data_root / "tasks.json")},
        {
            row["id"]: row
            for row in read_json(data_root / "interaction_variants.json")
        },
    )


def unresolved_by_task(v2_root: Path) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    path = v2_root / "unresolved.jsonl"
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "task_id" not in row or "finding_index" not in row:
            continue
        try:
            index = int(row["finding_index"])
        except (TypeError, ValueError):
            continue
        if index not in result.setdefault(row["task_id"], []):
            result[row["task_id"]].append(index)
    # Some model responses used the legacy decision shape
    # {index, status, reason} instead of writing finding_index rows to
    # unresolved.jsonl. Recover those decisions directly from the cache.
    responses = v2_root / "responses"
    for path in responses.glob("*.json") if responses.exists() else []:
        try:
            response = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if response.get("status") != "complete":
            continue
        task_id = response.get("task_id") or path.stem
        for decision in response.get("decisions", []):
            status = decision.get("decision", decision.get("status"))
            if status != "unresolved":
                continue
            index = decision.get("finding_index", decision.get("index"))
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            if index not in result.setdefault(task_id, []):
                result[task_id].append(index)
    return result


def finding_records(
    task_id: str, indexes: list[int], review_dir: Path
) -> list[dict[str, Any]]:
    path = review_dir / "responses" / "reference" / f"{task_id}.json"
    if not path.exists():
        return []
    output = read_json(path).get("output") or {}
    findings = output.get("findings") or []
    return [
        {"index": index, **findings[index - 1]}
        for index in indexes
        if 1 <= index <= len(findings)
    ]


def resolve_stage_evidence(
    task: dict[str, Any],
    findings: list[dict[str, Any]],
    snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    stages = (task.get("scenario") or {}).get("stages") or []
    combined = json.dumps(findings, ensure_ascii=False)
    selected = stage_numbers(combined, len(stages))
    if not selected:
        selected = set(range(len(stages)))
    output = []
    for stage_index in sorted(selected):
        stage = stages[stage_index]
        for evidence_index, evidence in enumerate(stage.get("evidence") or []):
            reference = evidence.get("reference") or {}
            source = reference.get("source")
            row = {
                "stage": stage_index,
                "evidence_index": evidence_index,
                "explanation": evidence.get("explanation"),
                "reference": copy.deepcopy(reference),
                "expected": copy.deepcopy(evidence.get("expected")),
                "public_tool": evidence.get("public_tool"),
                "result_pointer": evidence.get("result_pointer"),
                "status": "needs_replay",
            }
            if source == "snapshot" and snapshot is not None:
                actual, error = resolve_snapshot_pointer(
                    snapshot, reference.get("pointer", "")
                )
                row["actual"] = actual
                row["status"] = (
                    "verified"
                    if error is None and equal_value(actual, evidence.get("expected"))
                    else "contradicted"
                    if error is None
                    else "missing"
                )
                if error:
                    row["error"] = error
            output.append(row)
    return output


def resolve_finding_pointers(
    findings: list[dict[str, Any]],
    snapshot: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    output = []
    for finding in findings:
        text = " ".join(
            str(finding.get(key, ""))
            for key in ("title", "evidence", "impact", "recommendation")
        )
        matches = []
        for pointer in extract_pointers(text):
            row = {"pointer": pointer, "status": "missing"}
            if snapshot is not None:
                actual, error = resolve_snapshot_pointer(snapshot, pointer)
                row["actual"] = actual
                row["status"] = "verified" if error is None else "missing"
                if error:
                    row["error"] = error
            matches.append(row)
        output.append({"finding_index": finding.get("index"), "pointers": matches})
    return output


def build_packet(
    *,
    task_id: str,
    meta: dict[str, Any],
    task: dict[str, Any],
    source_task: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    data_root: Path,
) -> dict[str, Any]:
    evidence_task = source_task if meta["kind"] in {
        "repetition",
        "mind_change",
        "addtask",
        "modify",
        "cancel",
    } and source_task else task
    snapshot_id = (evidence_task.get("scenario") or {}).get("snapshot")
    snapshot = (
        load_snapshot(snapshot_id, data_root)
        if snapshot_id
        else None
    )
    stage_evidence = resolve_stage_evidence(evidence_task, findings, snapshot)
    pointer_evidence = resolve_finding_pointers(findings, snapshot)
    # Stage evidence carries an explicit authoring reference and expected
    # value. Paths extracted from reviewer prose are only hints: prose often
    # mentions relative paths such as /messages or /reward that are not rooted
    # snapshot pointers. Do not let those hints invalidate verified evidence.
    authoritative = [row["status"] for row in stage_evidence]
    status = (
        "contradicted"
        if "contradicted" in authoritative
        else "missing"
        if "missing" in authoritative
        else "verified"
        if authoritative and all(item == "verified" for item in authoritative)
        else "needs_replay"
    )
    return {
        "version": VERSION,
        "task_id": task_id,
        "kind": meta["kind"],
        "source_task_id": meta["source_task_id"],
        "snapshot_id": snapshot_id,
        "evidence_status": status,
        "findings": findings,
        "stage_evidence": stage_evidence,
        "finding_pointers": pointer_evidence,
        "source_task_context": {
            "source_task_id": meta["source_task_id"],
            "source_sha256": (source_task or {}).get("scenario", {}).get(
                "source_sha256"
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, default=V2)
    parser.add_argument("--review-dir", type=Path, default=REVIEW)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    meta_by_id = inventory(args.review_dir)
    unresolved = unresolved_by_task(args.input_root)
    tasks, variants = load_current_targets(args.input_root, args.data_root)
    rows = []
    for task_id, indexes in sorted(unresolved.items()):
        if args.limit is not None and len(rows) >= args.limit:
            break
        meta = meta_by_id.get(task_id)
        if not meta:
            continue
        target = variants.get(task_id, tasks.get(task_id))
        source = tasks.get(meta["source_task_id"])
        if target is None:
            continue
        findings = finding_records(task_id, indexes, args.review_dir)
        packet = build_packet(
            task_id=task_id,
            meta=meta,
            task=target,
            source_task=source,
            findings=findings,
            data_root=args.data_root,
        )
        out_path = args.out / "source-evidence" / f"{task_id}.json"
        write_json(out_path, packet)
        rows.append(
            {
                "task_id": task_id,
                "kind": meta["kind"],
                "finding_indexes": indexes,
                "evidence_status": packet["evidence_status"],
                "source_evidence_path": str(out_path),
            }
        )
        if len(rows) % args.batch_size == 0:
            write_json(
                args.out / "source-evidence" / f"batch-{len(rows) // args.batch_size:04d}.json",
                {"version": VERSION, "tasks": rows[-args.batch_size :]},
            )
    (args.out / "source-evidence-index.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    )
    write_json(
        args.out / "source-evidence-summary.json",
        {
            "version": VERSION,
            "tasks": len(rows),
            "evidence_status": {
                status: sum(row["evidence_status"] == status for row in rows)
                for status in ("verified", "contradicted", "missing", "needs_replay")
            },
            "batch_size": args.batch_size,
        },
    )


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""Merge approved continuations into native Gaia2 tasks, archiving authoring data.

This packages data, not a new acceptance run or a live-runtime certification.
No model/provider calls, work-record changes, or journal rewrites are performed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import shutil
from collections import Counter
from pathlib import Path


def content_hash(value):
    """Return the existing canonical JSON content digest."""
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def native_payload(task, index, original_id, initial_request):
    """Keep native questions/answers together and omit authoring sidecars."""
    payload = task.model_dump(mode="json")
    payload["id"] = original_id
    stages = []
    for part in index:
        stage = {
            "stage": part["stage"],
            "request": initial_request if part["stage"] == 0 else part["request"],
            "event_ids": part["event_ids"],
            "criteria": part["criteria"],
            "relation": part.get("relation"),
            "preconditions": [],
            "evidence": [],
            "checks": [],
        }
        for key in ("relation", "preconditions", "evidence", "checks"):
            if key in part:
                stage[key] = copy.deepcopy(part[key])
        stages.append(stage)
    payload["scenario"]["stages"] = stages
    return payload


def merge(dataset: Path, target: Path, archive: Path) -> dict:
    """Resolve the approved partition, compile 800 tasks, and install atomically."""
    import sys

    dataset, target, archive = (p.resolve() for p in (dataset, target, archive))
    if archive.exists():
        raise ValueError("Archive path already exists; never overwrite history")
    manifest = json.loads((dataset / "manifest.json").read_text())
    runtime = Path(manifest["manual_runtime"])
    sys.path.insert(0, str(runtime))
    package = importlib.import_module("tau2.task_authoring.gaia2_series")
    package.__path__.append(
        str(
            Path(__file__).resolve().parents[1] / "src/tau2/task_authoring/gaia2_series"
        )
    )
    from tau2.task_authoring.gaia2_series.compiler import compile_series
    from tau2.task_authoring.gaia2_series.pipeline import dataset_lock
    from tau2.task_authoring.gaia2_series.schema import SeriesDraft
    from tau2.task_authoring.gaia2_series.storage import (
        digest,
        file_digest,
        read_record,
        source_tasks,
        write_json,
    )

    with dataset_lock(dataset):
        _, tasks = source_tasks(dataset)
        manual = json.loads((dataset / "manual-scope.json").read_text())
        light = Path(manual["source_dataset"])
        scope = json.loads((light / "scope.json").read_text())
        audited = Path(scope["source_dataset"])
        groups = [
            set(manual["failed_ids"]),
            set(manual["preserved_passed_ids"]),
            set(scope["preserved_passed_repair_ids"]),
            set(scope["passed_ids"]),
        ]
        if len(set.union(*groups)) != 800 or sum(map(len, groups)) != 800:
            raise ValueError("Approved source partition overlaps or is incomplete")
        current_ids = [t["id"] for t in json.loads((target / "tasks.json").read_text())]
        if set(current_ids) != set(tasks) or len(current_ids) != 800:
            raise ValueError("Canonical original task IDs differ from source")
        selected, provenance, source_counts = [], [], Counter()
        for tid in current_ids:
            if tid in groups[0]:
                root, kind = dataset, "manual_review"
                record = read_record(root, tid)
                if record.get("manual_status") != "verified":
                    raise ValueError(f"Manual task is not verified: {tid}")
                raw = record["draft"]
            elif tid in groups[1]:
                root, kind = light, "reviewed_work"
                record = read_record(root, tid)
                if record.get("status") != "reviewed":
                    raise ValueError(f"Inherited work not reviewed: {tid}")
                raw = record["draft"]
            elif tid in groups[2]:
                root, kind = light, "passed_session"
                record = json.loads(
                    (root / "sessions" / f"{digest(tid)}.json").read_text()
                )
                if record.get("status") != "passed":
                    raise ValueError(f"Preserved session not passed: {tid}")
                raw = record["verified_prefix"]
            else:
                root, kind = audited, "audited_inherited"
                record = read_record(root, tid)
                if record.get("status") != "reviewed":
                    raise ValueError(f"Audited inherited task not reviewed: {tid}")
                raw = record["draft"]
            draft = SeriesDraft.model_validate(raw)
            verification = record["verification"]
            if (
                len(draft.stages) != 2
                or not verification.get("valid")
                or not verification.get("replay_verified")
                or verification.get("content_sha256") != digest(draft)
            ):
                raise ValueError(f"Missing complete content-bound execution: {tid}")
            task, index = compile_series(
                tasks[tid],
                draft,
                allow_current_evidence=verification.get("validation_mode")
                == "lightweight",
            )
            payload = native_payload(
                task, index, tid, tasks[tid].user_scenario.instructions.reason_for_call
            )
            selected.append(payload)
            source_counts[kind] += 1
            provenance.append(
                {
                    "task_id": tid,
                    "source": str(root),
                    "record_kind": kind,
                    "candidate_sha256": digest(draft),
                    "base_task_sha256": digest(tasks[tid]),
                    "author_attribution": "DeepSeek with assistant review"
                    if tid
                    in json.loads(
                        (
                            dataset.parent.parent
                            / "gaia2-deepseek-remaining-20261105"
                            / "human-review-progress.json"
                        ).read_text()
                    )["reviews"]
                    else "inherited generation/review; see archived source",
                }
            )

        # Complete all checks before touching the canonical task files.
        for payload in selected:
            stages = payload["scenario"]["stages"]
            graph = payload["scenario"]["events"]
            if [s["stage"] for s in stages] != [0, 1, 2]:
                raise ValueError("Missing native stage data")
            if len({e["id"] for e in graph}) != len(graph):
                raise ValueError("Duplicate event IDs")
            if set(e["id"] for e in graph) != {
                i for s in stages for i in s["event_ids"]
            }:
                raise ValueError("Stage index does not partition the graph")

        archive.mkdir(parents=True)
        # Only native data is backed up here; large assets/snapshots stay intact.
        backup = archive / "original-domain"
        backup.mkdir()
        for name in (
            "tasks.json",
            "manifest.json",
            "split_tasks.json",
            "verification.json",
        ):
            if (target / name).exists():
                shutil.copy2(target / name, backup / name)
        write_json(archive / "generation-provenance.json", provenance)
        write_json(
            archive / "integration-checks.json",
            {
                "tasks": 800,
                "requests": 2400,
                "source_counts": dict(source_counts),
                "model_calls": 0,
                "original_tasks_sha256": file_digest(backup / "tasks.json"),
                "live_runtime_certified": False,
            },
        )
        source = Path(manifest["source_root"])
        for snapshot in {t["scenario"]["snapshot"] for t in selected}:
            path = target / "snapshots" / f"{snapshot}.json.gz"
            original = source / "snapshots" / path.name
            if path.exists() and file_digest(path) != file_digest(original):
                raise ValueError("Existing snapshot differs from sealed source")
            if not path.exists():
                shutil.copy2(original, path)
        new_manifest = json.loads((target / "manifest.json").read_text())
        new_manifest.update(
            tasks=800,
            requests_per_task=3,
            complete=True,
            verified=False,
            execution_ready=False,
            status="integrated_pending_live_runtime",
            runtime_sha256=manifest["runtime_sha256"],
            tasks_sha256=hashlib.sha256(
                json.dumps(
                    selected, ensure_ascii=False, indent=2, allow_nan=False
                ).encode()
                + b"\n"
            ).hexdigest(),
            integration_note="Native task graph and private stage answers are integrated. Live stage delivery, early termination and stage-aware grading still require integration testing.",
        )
        write_json(target / "tasks.json", selected)
        write_json(target / "manifest.json", new_manifest)
        # Old verification describes the original task bytes, so archive it.
        (target / "verification.json").unlink(missing_ok=True)
        write_json(
            archive / "locations.json",
            {
                "canonical_dataset": str(target),
                "manual_dataset": str(dataset),
                "light_dataset": str(light),
                "audited_dataset": str(audited),
                "generation_runtime": str(runtime),
                "original_domain_backup": str(backup),
            },
        )
        return {
            "tasks": 800,
            "requests": 2400,
            "source_counts": dict(source_counts),
            "target": str(target),
            "archive": str(archive),
            "execution_ready": False,
        }


def main():
    """Merge into the existing domain only when explicitly invoked."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            merge(args.dataset, args.target, args.archive), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()

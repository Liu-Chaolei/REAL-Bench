#!/usr/bin/env python3
"""Export deterministic Gaia2 reference-operation results for later ASR use.

This collector executes the frozen reference scenario in ``runtime_mode=reference``.
It makes no model calls and never runs an Agent.  Each task file contains resolved
reference arguments, tool responses, reference Agent replies, stage membership,
and the complete replay journal.  The output is intentionally separate from the
sealed task data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from tau2.data_model.message import ToolCall
from tau2.domains.gaia2.environment import get_environment, get_tasks
from tau2.environment.scenario import resolve_references

VERSION = "gaia2-reference-results-v1"


def _write_json(path: Path, value: dict) -> None:
    """Write one private result atomically and avoid partial task files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".writing")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    temporary.replace(path)
    os.chmod(path, 0o600)


def _stage_index(task) -> dict[str, int]:
    """Map every scenario event to its declared stage, when applicable."""
    result = {}
    for stage in task.scenario.stages:
        for event_id in stage.event_ids:
            result[event_id] = stage.stage
    return result


def _collect_one(task_payload: dict, root: str, output_dir: str) -> dict:
    """Execute and export one task in an isolated worker process."""
    from tau2.data_model.tasks import Task

    task = Task.model_validate(task_payload)
    output_path = Path(output_dir) / "tasks" / f"{task.id}.json"
    environment = None
    try:
        environment = get_environment(
            task=task,
            root=Path(root),
            allow_unverified=True,
            stage_control=False,
            runtime_mode="reference",
        )
        runtime = environment.scenario_runtime
        runtime.advance(0)
        pending = list(task.scenario.events)
        responses = {}
        reference_replies = {}

        while pending:
            pending = [event for event in pending if event.id not in runtime.completed]
            if not pending:
                break
            ready = [
                event
                for event in pending
                if all(parent in runtime.completed for parent in event.dependencies)
            ]
            if not ready:
                raise ValueError("Reference fixture has unresolved dependencies")
            ready.sort(key=runtime.event_priority)
            event = ready[0]
            due = (
                max(
                    (runtime.completed[parent] for parent in event.dependencies),
                    default=0,
                )
                + event.delay_seconds
            )
            runtime.advance(max(runtime.elapsed, due))
            if event.id in runtime.completed:
                continue
            arguments = resolve_references(event.arguments, runtime.results)
            if event.kind == "user":
                runtime.delivered("user", arguments["content"], event_id=event.id)
            elif event.channel == "agent":
                content = arguments["content"]
                runtime.delivered("agent", content)
                reference_replies[event.id] = {"content": content}
            elif event.kind == "reference":
                response = environment.get_response(
                    ToolCall(
                        id=event.id,
                        name=event.tool,
                        arguments=arguments,
                        requestor="assistant",
                    )
                )
                responses[event.id] = {
                    "content": response.content,
                    "error": response.error,
                    "execution_status": response.execution_status,
                }
                if response.error:
                    raise ValueError(f"{event.tool}: {response.content}")
            else:
                # Usually completed by runtime.advance() above.  Keep this branch
                # explicit so a future environment event cannot be silently lost.
                runtime.advance(runtime.elapsed)
            if event.id not in runtime.completed:
                raise ValueError(f"Reference outcome did not bind: {event.id}")

        if len(runtime.completed) < len(task.scenario.events):
            missing = [
                event.id
                for event in task.scenario.events
                if event.id not in runtime.completed
            ]
            raise ValueError(f"Reference fixture left events incomplete: {missing}")

        stages = _stage_index(task)
        events = []
        for event in task.scenario.events:
            resolved = None
            if event.id in runtime.results or event.kind in {"reference", "user"}:
                try:
                    resolved = resolve_references(event.arguments, runtime.results)
                except KeyError:
                    resolved = None
            events.append(
                {
                    "event_id": event.id,
                    "stage": stages.get(event.id),
                    "kind": event.kind,
                    "channel": event.channel,
                    "tool": event.tool,
                    "dependencies": list(event.dependencies),
                    "arguments": deepcopy(event.arguments),
                    "resolved_arguments": resolved,
                    "result": deepcopy(runtime.results.get(event.id)),
                    "completed_time": runtime.completed.get(event.id),
                    "tool_response": responses.get(event.id),
                    "reference_reply": reference_replies.get(event.id),
                }
            )
        journal = [record.model_dump(mode="json") for record in runtime.records]
        payload = {
            "version": VERSION,
            "task_id": task.id,
            "source_sha256": task.scenario.source_sha256,
            "snapshot": task.scenario.snapshot,
            "category": task.scenario.category,
            "stages": [
                {
                    "stage": stage.stage,
                    "request": stage.request,
                    "event_ids": list(stage.event_ids),
                    "events": [
                        event["event_id"]
                        for event in events
                        if event["stage"] == stage.stage
                    ],
                }
                for stage in task.scenario.stages
            ],
            "events": events,
            "reference_replies": [
                event for event in events if event["reference_reply"] is not None
            ],
            "journal": journal,
            "counts": {
                "scenario_events": len(task.scenario.events),
                "completed_events": len(runtime.completed),
                "reference_tool_events": len(responses),
                "reference_replies": len(reference_replies),
                "journal_records": len(journal),
            },
        }
        _write_json(output_path, payload)
        return {
            "task_id": task.id,
            "status": "ok",
            "path": str(output_path),
            "counts": payload["counts"],
        }
    except Exception as error:
        failure = {
            "version": VERSION,
            "task_id": task.id,
            "status": "error",
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        }
        _write_json(output_path, failure)
        return {
            "task_id": task.id,
            "status": "error",
            "path": str(output_path),
            "error_type": type(error).__name__,
            "error": str(error),
        }
    finally:
        if environment is not None:
            environment.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("data/tau2/domains/gaia2"),
        help="Gaia2 data root.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/gaia2_stage_hotwords/v1/gt_results"),
        help="Output directory kept separate from sealed task data.",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--task-ids", nargs="*", default=None)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse already successful per-task files with the same version.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    tasks = get_tasks(root=root)
    if args.task_ids:
        selected = set(args.task_ids)
        tasks = [task for task in tasks if task.id in selected]
        missing = selected - {task.id for task in tasks}
        if missing:
            parser.error(f"Unknown task IDs: {sorted(missing)}")
    output.mkdir(parents=True, exist_ok=True)
    task_dir = output / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    work = []
    for task in tasks:
        path = task_dir / f"{task.id}.json"
        if args.resume and path.exists():
            try:
                existing = json.loads(path.read_text())
                if (
                    existing.get("version") == VERSION
                    and existing.get("status") == "ok"
                ):
                    work.append(
                        {
                            "task_id": task.id,
                            "status": "reused",
                            "path": str(path),
                            "counts": existing.get("counts"),
                        }
                    )
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        work.append((task, task.model_dump(mode="json")))

    results = [item for item in work if isinstance(item, dict)]
    jobs = [item for item in work if isinstance(item, tuple)]
    if jobs:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(_collect_one, payload, str(root), str(output)): task.id
                for task, payload in jobs
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(
                    f"{len(results)}/{len(work)} {result['task_id']} {result['status']}",
                    flush=True,
                )
    results.sort(key=lambda row: row["task_id"])
    task_bytes = (root / "tasks.json").read_bytes()
    manifest = {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "tasks_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "task_count": len(tasks),
        "selected_task_count": len(results),
        "ok_count": sum(row["status"] in {"ok", "reused"} for row in results),
        "error_count": sum(row["status"] == "error" for row in results),
        "results": results,
    }
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "version",
                    "task_count",
                    "selected_task_count",
                    "ok_count",
                    "error_count",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if manifest["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

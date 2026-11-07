"""Key-free, complete coverage validation for converted Gaia2 tasks."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from tau2.data_model.tasks import Task
from tau2.domains.gaia2.utils import read_snapshot


def _check_reference(task_payload, root, assets, observer_factory=None):
    from tau2.data_model.message import ToolCall
    from tau2.domains.gaia2.environment import get_environment
    from tau2.domains.gaia2.scoring import check_goal
    from tau2.environment.replay import replay_environment
    from tau2.environment.scenario import resolve_references

    task = Task.model_validate(task_payload)
    environment = None
    replayed = None
    try:
        environment = get_environment(
            task=task,
            root=root,
            allow_unverified=True,
            stage_control=False,
            runtime_mode="reference",
        )
        if observer_factory is not None:
            observer_factory(environment)
        runtime = environment.scenario_runtime
        pending = list(task.scenario.events)
        stage_states, checked_stages, answers = {}, set(), {}
        check_count = 0
        authoring_failures = []
        runtime.advance(0)
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
            event = min(ready, key=runtime.event_priority)
            due = (
                max((runtime.completed[p] for p in event.dependencies), default=0)
                + event.delay_seconds
            )
            runtime.advance(max(runtime.elapsed, due))
            if event.id in runtime.completed:
                continue
            arguments = resolve_references(event.arguments, runtime.results)
            if event.kind == "user":
                runtime.delivered("user", arguments["content"], event_id=event.id)
            elif event.channel == "agent":
                runtime.delivered("agent", arguments["content"])
                answers[event.id] = {"content": arguments["content"]}
            elif event.kind == "reference":
                result = environment.get_response(
                    ToolCall(
                        id=event.id,
                        name=event.tool,
                        arguments=arguments,
                        requestor="assistant",
                    )
                )
                if result.error:
                    raise ValueError(f"{event.tool}: {result.content}")
            if event.id not in runtime.completed:
                raise ValueError(f"Reference outcome did not bind: {event.tool}")
            for stage in task.scenario.stages:
                if (
                    stage.stage in checked_stages
                    or not set(stage.event_ids) <= runtime.completed.keys()
                ):
                    continue
                from copy import deepcopy

                from tau2.task_authoring.gaia2_series.schema import OutcomeCheck
                from tau2.task_authoring.gaia2_series.verification import (
                    Execution,
                    evaluate_check,
                )

                environment.tools.refresh_db(include_files=True)
                stage_states[stage.stage] = deepcopy(environment.tools.db.apps)
                from tau2.task_authoring.gaia2_series.materials import app_states

                execution = Execution(
                    stage_states,
                    runtime.results,
                    [],
                    [],
                    app_states(environment.tools.source_snapshot),
                    {},
                    answers,
                )
                for check in stage.checks:
                    from tau2.task_authoring.gaia2_series.failures import (
                        CandidateValidationError,
                    )

                    try:
                        evaluate_check(OutcomeCheck.model_validate(check), execution)
                    except CandidateValidationError as error:
                        authoring_failures.append(
                            {"stage": stage.stage, "error": str(error)}
                        )
                    check_count += 1
                checked_stages.add(stage.stage)
        if not check_goal(environment.tools):
            raise ValueError("Reference fixture failed the target business state")
        replayed = get_environment(
            task=task,
            root=root,
            allow_unverified=True,
            stage_control=False,
            runtime_mode="reference",
        )
        replay_environment(
            replayed, [record.model_dump(mode="json") for record in runtime.records]
        )
        if not check_goal(replayed.tools):
            raise ValueError("Replayed fixture failed the target business state")
        return {
            "id": task.id,
            "valid": len(runtime.results) == len(task.scenario.events),
            "operations": len(runtime.results),
            "authoring_checks": check_count,
            "authoring_check_failures": authoring_failures,
            "replay_verified": True,
        }
    except Exception as error:
        return {
            "id": task.id,
            "valid": False,
            "error": f"{type(error).__name__}: {error}",
        }
    finally:
        if environment is not None:
            environment.close()
        if replayed is not None:
            replayed.close()


def _resource_paths(value):
    if isinstance(value, dict):
        if value.get("type") == "file" and value.get("real_path"):
            yield value["real_path"]
        for child in value.values():
            yield from _resource_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _resource_paths(child)


def verify_dataset(root: Path, *, reference=False, workers=4, seal=False) -> dict:
    """Verify every task explicitly; unsupported items remain in the report."""
    from tau2.domains.gaia2.environment import get_tasks, get_tasks_split
    from tau2.domains.gaia2.utils import runtime_digest

    initial_runtime = runtime_digest()

    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    tasks = get_tasks(root=root)
    splits = get_tasks_split(root=root)
    errors = []
    if len(tasks) != 800 or len({t.id for t in tasks}) != 800:
        errors.append("Expected exactly 800 unique core tasks")
    if set(splits["base"]) != {t.id for t in tasks}:
        errors.append("Base split differs from the imported population")
    if (
        hashlib.sha256((root / "tasks.json").read_bytes()).hexdigest()
        != manifest["tasks_sha256"]
    ):
        errors.append("Task artifact hash differs from the import manifest")
    resources = set()
    for digest in {t.scenario.snapshot for t in tasks}:
        resources.update(_resource_paths(read_snapshot(digest, root)))
    assets = Path(manifest["assets"]).resolve()
    missing = []
    resource_hashes = {}
    for relative in sorted(resources):
        path = (assets / relative).resolve()
        if not path.is_relative_to(assets):
            errors.append(f"Asset path escapes the resource root: {relative}")
        elif not path.is_file():
            missing.append(relative)
        else:
            with path.open("rb") as stream:
                resource_hashes[relative] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    references = []
    if reference:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _check_reference, t.model_dump(mode="json"), str(root), str(assets)
                )
                for t in tasks
            ]
            for future in as_completed(futures):
                references.append(future.result())
                if len(references) % 50 == 0:
                    print(
                        f"Verified reference operations: {len(references)}/{len(tasks)}",
                        flush=True,
                    )
        references.sort(key=lambda item: item["id"])
    if runtime_digest() != initial_runtime:
        errors.append(
            "Runtime changed during verification; repeat against one immutable implementation"
        )
    result = {
        "valid": not errors and not missing and all(r["valid"] for r in references),
        "tasks": len(tasks),
        "runtime_sha256": initial_runtime,
        "tasks_sha256": manifest["tasks_sha256"],
        "errors": errors,
        "missing_resources": missing,
        "resource_hashes": resource_hashes,
        "reference_checked": len(references),
        "reference_failures": [r for r in references if not r["valid"]],
        "authoring_checks": sum(r.get("authoring_checks", 0) for r in references),
        "authoring_check_failures": [
            {"id": row["id"], **failure}
            for row in references
            for failure in row.get("authoring_check_failures", [])
        ],
    }
    (root / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    if seal:
        if not result["valid"] or not reference or len(references) != 800:
            raise ValueError(
                "Only complete reference/replay verification can seal task data"
            )
        from tau2.domains.gaia2.cli import publish_binding
        from tau2.domains.gaia2.utils import canonical_json, runtime_digest
        from tau2.orchestrator.scenario_stages import SERIES_VERSION

        assets_sha256 = hashlib.sha256(canonical_json(resource_hashes)).hexdigest()
        current_runtime = runtime_digest()
        for task in tasks:
            task.scenario.runtime_sha256 = current_runtime
            task.scenario.assets_sha256 = assets_sha256
        (root / "resources.json").write_bytes(canonical_json(resource_hashes))
        (root / "tasks.json").write_bytes(
            canonical_json([task.model_dump(mode="json") for task in tasks])
        )
        from tau2.domains.gaia2.replies import REPLY_VERSION
        from tau2.evaluator.outcome import GRADING_VERSION

        manifest.update(
            reply_runtime_version=REPLY_VERSION,
            grading_version=GRADING_VERSION,
            verified=True,
            execution_ready=True,
            status="ready_for_live_series_pilot"
            if any(t.scenario.stages for t in tasks)
            else "verified",
            series_runtime_version=SERIES_VERSION,
            complete=True,
            runtime_sha256=current_runtime,
            assets_sha256=assets_sha256,
            tasks_sha256=hashlib.sha256((root / "tasks.json").read_bytes()).hexdigest(),
        )
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        result.update(
            tasks_sha256=manifest["tasks_sha256"],
            assets_sha256=assets_sha256,
            sealed=True,
        )
        (root / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
        publish_binding(root)
    return result


def verify_pinned_resources(root: Path, expected: str | None) -> None:
    """Validate resource hashes before startup, with stat-keyed digest caching."""
    from tau2.domains.gaia2.tools import _file_digest
    from tau2.domains.gaia2.utils import canonical_json

    root = Path(root)
    hashes = json.loads((root / "resources.json").read_text())
    if hashlib.sha256(canonical_json(hashes)).hexdigest() != expected:
        raise ValueError("Gaia2 resource manifest differs from the task's pin")
    assets = Path(json.loads((root / "manifest.json").read_text())["assets"]).resolve()
    for relative, digest in hashes.items():
        path = (assets / relative).resolve()
        if not path.is_relative_to(assets):
            raise ValueError("Resource path escapes its declared root")
        stat = path.stat()
        if _file_digest(str(path), stat.st_size, stat.st_mtime_ns) != digest:
            raise ValueError(f"Gaia2 resource changed: {relative}")

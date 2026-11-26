"""Offline, bounded source replay for Gaia2 repair packets.

No evaluator, reward or model calls are made here. A stage checkpoint includes
all events in that stage and their ancestors: replaying only the mentioned
writer could omit a competing write and manufacture misleading state evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

PROTOCOL = "source-replay-1"


def fingerprint(task: dict, root: Path) -> str:
    """Bind replay evidence to exact task, snapshot bytes and runtime sources."""
    digest = hashlib.sha256(json.dumps(task, sort_keys=True).encode())
    digest.update(PROTOCOL.encode())
    snapshot = root / "snapshots" / f"{task['scenario']['snapshot']}.json.gz"
    digest.update(snapshot.read_bytes())
    source = Path(__file__).resolve().parents[1] / "src/tau2"
    for path in (
        Path(__file__),
        source / "environment/scenario.py",
        source / "domains/gaia2/environment.py",
        source / "domains/gaia2/tools.py",
    ):
        digest.update(path.read_bytes())
    # Bind application semantics too, including uncommitted vendor changes.
    for path in sorted((source / "domains/gaia2/vendor").glob("apps/*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def pointer(value: Any, path: str) -> Any:
    """Resolve an RFC 6901 pointer without coercing booleans or missing values."""
    if path and not path.startswith("/"):
        raise ValueError("Invalid JSON pointer")
    for part in path.split("/")[1:] if path else []:
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def reference_value(ref: dict, *, initial: dict, states: dict, results: dict) -> Any:
    """Resolve snapshot/state/result references, including returned object IDs."""
    if ref["source"] == "snapshot":
        value = initial
    elif ref["source"] == "state":
        value = states[ref.get("stage", 0)]
    elif ref["source"] == "result":
        value = results[ref["event_id"]]
    else:
        raise ValueError("Unknown reference source")
    value = pointer(value, ref.get("pointer", ""))
    key_ref = ref.get("key_from_result")
    if key_ref:
        key = pointer(results[key_ref["event_id"]], key_ref.get("pointer", ""))
        value = pointer(value[key], ref.get("suffix_pointer", ""))
    return copy.deepcopy(value)


def required_events(task: dict, rows: list[dict]) -> set[str]:
    """Collect checkpoint events plus explicit and result-template ancestors."""
    spec = task["scenario"]
    events = {event["id"]: event for event in spec["events"]}
    if len(events) != len(spec["events"]):
        raise ValueError("Duplicate event IDs")
    stages = {stage["stage"]: stage for stage in spec.get("stages", [])}
    wanted = set()
    for row in rows:
        ref = row["reference"]
        if ref["source"] == "state":
            wanted.update(stages[ref.get("stage", 0)]["event_ids"])
        if ref.get("event_id"):
            wanted.add(ref["event_id"])
        if ref.get("key_from_result"):
            wanted.add(ref["key_from_result"]["event_id"])
    queue = list(wanted)
    while queue:
        event_id = queue.pop()
        event = events[event_id]
        parents = set(event.get("dependencies", []))
        parents.update(
            re.findall(r"{{([^{}]+)}}", json.dumps(event.get("arguments", {})))
        )
        for parent in parents - wanted:
            wanted.add(parent)
            queue.append(parent)
    return wanted


def replay_evidence(task: dict, rows: list[dict], root: Path) -> dict:
    """Execute the needed reference prefix in a disposable local environment.

    Partial results survive a failed tool/event. Unreached checkpoints remain
    missing and are never substituted with the initial snapshot.
    """
    from tau2.data_model.message import ToolCall
    from tau2.data_model.tasks import Task
    from tau2.domains.gaia2.environment import get_environment
    from tau2.environment.scenario import resolve_references

    environment = None
    states, answers, results = {}, {}, {}
    completed_events = []
    error = None
    initial = {}
    try:
        wanted = required_events(task, rows)
        model = Task.model_validate(task)
        environment = get_environment(
            task=model,
            root=root,
            allow_unverified=True,
            stage_control=False,
            runtime_mode="reference",
        )
        initial = {
            app["name"]: copy.deepcopy(app["app_state"])
            for app in environment.tools.source_snapshot
        }
        runtime = environment.scenario_runtime
        stages = model.scenario.stages
        runtime.advance(0)
        pending = [event for event in model.scenario.events if event.id in wanted]

        def checkpoints():
            for stage in stages:
                if (
                    stage.stage not in states
                    and set(stage.event_ids) <= runtime.completed.keys()
                ):
                    environment.tools.refresh_db(include_files=True)
                    states[stage.stage] = copy.deepcopy(environment.tools.db.apps)

        checkpoints()
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
                raise ValueError("Unresolved or cyclic reference dependencies")
            event = min(ready, key=runtime.event_priority)
            due, _ = runtime.event_priority(event)
            runtime.advance(max(runtime.elapsed, due))
            if event.id not in runtime.completed:
                arguments = resolve_references(event.arguments, runtime.results)
                if event.kind == "user":
                    runtime.delivered("user", arguments["content"], event_id=event.id)
                elif event.channel == "agent":
                    runtime.delivered("agent", arguments["content"])
                    answers[event.id] = {"content": arguments["content"]}
                elif event.kind == "reference":
                    response = environment.get_response(
                        ToolCall(
                            id=event.id,
                            name=event.tool,
                            arguments=arguments,
                            requestor="assistant",
                        )
                    )
                    if response.error:
                        raise ValueError(f"{event.id}: {response.content}")
                if event.id not in runtime.completed:
                    raise ValueError(f"Reference outcome did not bind: {event.id}")
            completed_events.append(event.id)
            checkpoints()
        results = copy.deepcopy(runtime.results)
        results.update(answers)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if environment is not None:
            results = copy.deepcopy(environment.scenario_runtime.results)
            results.update(answers)
    finally:
        if environment is not None:
            environment.close()
    resolved = []
    for row in rows:
        record = copy.deepcopy(row)
        record["basis"] = "candidate_reference_replay"
        try:
            actual = reference_value(
                row["reference"],
                initial=initial,
                states=states,
                results=results,
            )
            record["actual"] = actual
            # Strict JSON equality; do not equate True with 1 or None with "None".
            record["status"] = (
                "verified"
                if (
                    json.dumps(actual, sort_keys=True)
                    == json.dumps(row.get("expected"), sort_keys=True)
                )
                else "contradicted"
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            record["status"] = "missing"
            record["error"] = f"{type(exc).__name__}: {exc}"
        resolved.append(record)
    return {
        "protocol": PROTOCOL,
        "completed_events": completed_events,
        "checkpoints": sorted(states),
        "error": error,
        "rows": resolved,
    }

#!/usr/bin/env python3
"""Exercise every actual-mail reply against real apps and strict journals, offline."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from tau2.data_model.message import ToolCall
from tau2.domains.gaia2.environment import get_environment, get_tasks
from tau2.domains.gaia2.replies import ActualMailReplies, reply_audit
from tau2.domains.gaia2.tools import json_value
from tau2.domains.gaia2.verify import _check_reference
from tau2.environment.replay import replay_environment
from tau2.environment.scenario import resolve_references


def verify_task(task):
    """Verify all migrated replies belonging to one task in an isolated worker."""
    results = []
    root = Path("data/tau2/domains/gaia2")
    assets = json.loads((root / "manifest.json").read_text())["assets"]
    captured = []

    def capture(env):
        captured.append(env)

    reference = _check_reference(
        task.model_dump(mode="json"), str(root), assets, capture
    )
    if not reference["valid"]:
        raise ValueError(reference)
    journal = [r.model_dump(mode="json") for r in captured[0].scenario_runtime.records]
    prototype = get_environment(task=task, allow_unverified=True, runtime_mode="online")
    rules = deepcopy(prototype.scenario_runtime.event_triggers.rules)
    prototype.close()
    for key, (event, origin, user) in rules.items():
        # Every reference outgoing call is an observed real application operation.
        stop = next(
            i
            for i, r in enumerate(journal)
            if r["kind"] == "tool" and r["call_id"] == origin.id
        )
        for variant in ["original", "individual"]:
            env = get_environment(
                task=task, allow_unverified=True, runtime_mode="replay"
            )
            replayed = None
            try:
                prefix = journal[:stop]
                replay_environment(env, prefix)
                rt = env.scenario_runtime
                record_offset = len(rt.records)

                def lookup(identifier):
                    for folder in env.tools.apps["Emails"].folders.values():
                        try:
                            return json_value(folder.get_email_by_id(identifier))
                        except (KeyError, ValueError, IndexError):
                            continue
                    raise ValueError(identifier)

                rt.event_triggers = ActualMailReplies(rt, lookup)
                # Scope this case to its correspondent; other replies have their own cases.
                rt.event_triggers.rules = {key: rt.event_triggers.rules[key]}
                rt.event_triggers.managed = {key}
                # Prevent legacy emission of other scripted events in this isolated case.
                rt.managed_user_events.update(
                    e.id
                    for e in rt.spec.events
                    if e.kind != "reference" and e.id != key
                )
                args = resolve_references(origin.arguments, rt.results)
                if variant == "individual" and origin.tool == "Emails__send_email":
                    args["recipients"] = [event.arguments["sender"]]
                response = env.get_response(
                    ToolCall(
                        id="actual-send",
                        name=origin.tool,
                        arguments=args,
                        requestor="assistant",
                    )
                )
                if response.error:
                    raise ValueError(response.content)
                rt.advance(rt.elapsed + event.delay_seconds)
                delivered = next(
                    r
                    for r in rt.records
                    if r.kind == "environment" and r.event_id == key
                )
                expected = (
                    json.loads(response.content)
                    if response.content.startswith('"')
                    else response.content
                )
                assert delivered.arguments["email_id"] == expected
                # Include the actual prefix; bind records remain observational.
                records = prefix + [
                    r.model_dump(mode="json") for r in rt.records[record_offset:]
                ]
                replayed = get_environment(
                    task=task, allow_unverified=True, runtime_mode="replay"
                )
                replay_environment(replayed, records)
                assert replayed.scenario_runtime.results[key] == delivered.result
                results.append(
                    {
                        "task_id": task.id,
                        "event_id": key,
                        "variant": variant,
                        "replay_verified": True,
                    }
                )
            finally:
                env.close()
                if replayed:
                    replayed.close()
    return results


def verify(output, workers=8):
    """Validate all reply rules using isolated workers without model calls."""
    from concurrent.futures import ProcessPoolExecutor, as_completed

    tasks = get_tasks()
    audit = reply_audit(tasks)
    selected = {r["task_id"] for r in audit["events"] if r["rule"] == "actual_mail"}
    results = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(verify_task, task): task.id
            for task in tasks
            if task.id in selected
        }
        for future in as_completed(futures):
            results.extend(future.result())
            print(
                f"Verified reply task {futures[future]} ({len(results)} cases)",
                flush=True,
            )
    output.mkdir(parents=True, exist_ok=True)
    (output / "event-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    )
    report = {
        "version": audit["version"],
        "events": audit["migrated_events"],
        "tasks": len(selected),
        "cases": len(results),
        "results": results,
    }
    (output / "reply-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    verify(args.output, args.workers)

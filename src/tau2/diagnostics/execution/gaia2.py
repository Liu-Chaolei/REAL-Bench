"""Gaia2 business operations and state effects adapted to tau's native journals."""

import copy
import re
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from tau2.diagnostics.execution.checks import canonical_tool, compare_args
from tau2.diagnostics.execution.effects import EffectEvaluator
from tau2.diagnostics.execution.matching import match_operations
from tau2.diagnostics.execution.scoring import (
    aggregate,
    dependency_edges,
    dimension,
    order_score,
    parameter_score,
    tool_score,
)


@lru_cache(maxsize=4)
def metadata(root):
    """Read operation metadata once; no model or reference action is executed."""
    import json

    from tau2.domains.gaia2.tools import Gaia2Tools
    from tau2.domains.gaia2.utils import read_snapshot

    manifest = json.loads((Path(root) / "manifest.json").read_text())
    tasks = json.loads((Path(root) / "tasks.json").read_text())
    result = {}
    for snapshot in sorted({t["scenario"]["snapshot"] for t in tasks}):
        tools = Gaia2Tools(
            read_snapshot(snapshot, Path(root)), assets=Path(manifest["assets"])
        )
        try:
            for name, tool in tools._operations.items():
                result[name] = {
                    "write": tool.write_operation,
                    "app": tool.class_instance.name,
                    "defaults": {a.name: a.default for a in tool.args if a.has_default},
                }
        finally:
            tools.close()
    return result


def canonical_name(name):
    app, function = name.split("__", 1)
    # makedirs and mkdir call the same native filesystem operation.
    if app == "Files" and function == "makedirs":
        function = "mkdir"
    if app == "InternalContacts":
        app = "Contacts"
    return canonical_tool(app, function)


def reference_operations(task, tools):
    refs = []
    stages = {key: st.stage for st in task.scenario.stages for key in st.event_ids}
    for event in task.scenario.events:
        if (
            event.kind != "reference"
            or event.channel == "agent"
            or event.tool.startswith("AgentUserInterface__")
        ):
            continue
        meta = tools.get(event.tool)
        if meta is None:
            raise ValueError("Missing Gaia2 tool metadata: " + event.tool)
        if not meta["write"]:
            continue
        refs.append(
            {
                "id": event.id,
                "tool": canonical_name(event.tool),
                "app": meta["app"],
                "args": {
                    **copy.deepcopy(meta.get("defaults", {})),
                    **copy.deepcopy(event.arguments),
                },
                "time": None,
                "stage": stages.get(event.id, 0),
            }
        )
    return refs


def frame(value, key):
    """Translate native snapshots into the published effect-validator format."""
    if value is None:
        return None
    result = {
        "stable": True,
        "apps": copy.deepcopy(value),
        "index": key,
        "file_evidence": {},
    }
    for app, state in value.items():
        if app != "Files" or "directories" not in state:
            continue
        nodes = {"": {"name": "", "type": "directory", "children": []}}
        entries = [(p, "directory") for p in state["directories"]] + [
            (p, "file") for p in state["files"]
        ]
        for path, kind in sorted(entries, key=lambda x: (x[0].count("/"), x[0])):
            parts = path.strip("/").split("/")
            for i in range(len(parts)):
                p = "/".join(parts[: i + 1])
                if p not in nodes:
                    node = {
                        "name": parts[i],
                        "type": kind if i == len(parts) - 1 else "directory",
                        "children": [],
                    }
                    nodes[p] = node
                    nodes["/".join(parts[:i])]["children"].append(node)
        result["apps"][app] = {"files": nodes[""]}
        result["file_evidence"][app] = {
            p.lstrip("/"): {"sha256": sha} for p, sha in state["files"].items()
        }
    return result


class Gaia2ExecutionAdapter:
    """Same scoring core for every Agent using the Gaia2 environment."""

    def __init__(self, tools):
        self.tools = tools

    def evaluate(self, task, evidence, judge):
        refs = reference_operations(task, self.tools)
        actual = [
            {
                "id": op.call_id,
                "tool": canonical_name(op.name),
                "app": op.app,
                "args": {
                    **copy.deepcopy(self.tools.get(op.name, {}).get("defaults", {})),
                    **op.arguments,
                },
                "raw_args": op.raw_arguments,
                "time": op.started,
                "succeeded": not op.error,
                "return_value": op.result,
                "evidence": op,
            }
            for op in evidence.operations
            if op.kind == "WRITE" and op.requestor == "assistant"
        ]
        # READ results are actual observations, never oracle answer substitutions.
        # Bind a reference READ only when its supplied arguments match and all
        # eligible observed returns agree. This resolves participant/user lookup
        # references without adding READs to the four-part score.
        read_results = {}
        pending_reads = [
            event
            for event in task.scenario.events
            if event.kind == "reference"
            and event.channel != "agent"
            and event.tool in self.tools
            and not self.tools[event.tool]["write"]
        ]
        token = re.compile(r"\{\{([^{}]+)\}\}")

        def resolve_reads(value):
            if isinstance(value, dict):
                return {k: resolve_reads(v) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve_reads(v) for v in value]
            if isinstance(value, str):
                full = token.fullmatch(value)
                if full and full[1] in read_results:
                    return copy.deepcopy(read_results[full[1]])
                return token.sub(
                    lambda m: str(read_results[m[1]]) if m[1] in read_results else m[0],
                    value,
                )
            return value

        from tau2.diagnostics.intent.io import canonical

        for _ in range(len(pending_reads) + 1):
            changed = False
            for event in pending_reads:
                if event.id in read_results:
                    continue
                args = resolve_reads(event.arguments)
                if "{{" in canonical(args):
                    continue
                consumers = [
                    ref
                    for ref in refs
                    if "{{" + event.id + "}}" in canonical(ref["args"])
                ]
                consumer_starts = [
                    call["time"]
                    for ref in consumers
                    for call in actual
                    if call["tool"] == ref["tool"] and call["app"] == ref["app"]
                ]
                cutoff = min(consumer_starts) if consumer_starts else float("inf")
                candidates = [
                    op.result
                    for op in evidence.operations
                    if op.name == event.tool
                    and not op.error
                    and op.ended < cutoff
                    and all(op.arguments.get(k) == v for k, v in args.items())
                ]
                if candidates and len({canonical(v) for v in candidates}) == 1:
                    read_results[event.id] = candidates[0]
                    changed = True
            if not changed:
                break
        refs = [dict(ref, args=resolve_reads(ref["args"])) for ref in refs]
        user = self.user(evidence)
        self.user_name = " ".join(
            str(user.get(k) or "") for k in ("first_name", "last_name")
        ).strip()
        cache = {}
        stage_times = {
            r["stage"]: r["started"]["time"]
            for r in evidence.stages.get("stages", [])
            if r.get("started")
        }

        def compare(ref, call, name=""):
            key = (ref["id"], call["id"])
            if key in cache:
                return copy.deepcopy(cache[key])
            rows = compare_args(ref["tool"], call["args"], ref["args"], self.user_name)
            for row in rows:
                row["matching_weight"] = (
                    1
                    if row["parameter"] == "$semantic_group"
                    else max(len(refs), len(actual)) + 1
                )
                if row["parameter"] == "$semantic_group" and any(
                    field not in call["args"] or call["args"][field] is None
                    for field in row["fields"]
                ):
                    row.update(passed=False, reason="required_text_parameter_missing")
                if (
                    row["parameter"] == "$semantic_group"
                    and row["passed"] is None
                    and not any("{{" in str(v) for v in row["expected"].values())
                ):
                    try:
                        row["passed"] = self.semantic(task, ref, row, judge, user)
                        row["reason"] = "native_are_semantic_group"
                    except Exception as exc:
                        row.update(
                            passed=None,
                            reason="semantic_unavailable:" + type(exc).__name__,
                        )
            cache[key] = copy.deepcopy(rows)
            return rows

        # Unreleased requirements cannot be satisfied by earlier unrelated work.
        # Partition matching candidates by release time without influencing order
        # or effects; missing-stage reference nodes have no eligible calls.
        eligible_actual = actual

        def eligible_compare(ref, call, name=""):
            return compare(ref, call, name)

        matching = match_operations(
            refs,
            eligible_actual,
            self.user_name,
            eligible_compare,
            eligibility=lambda ref, call: ref["app"] == call["app"]
            and (
                not evidence.stages
                or ref["stage"] in stage_times
                and call["time"] >= stage_times[ref["stage"]]
            ),
        )
        events = [
            {"event_id": e.id, "dependencies": e.dependencies}
            for e in task.scenario.events
        ]
        edges = dependency_edges(events, {r["id"] for r in refs})
        self.scenario = {"events": events}
        self.actual = actual
        self.evidence = evidence
        self.dynamic_membership = any(
            e.kind == "environment" and "participant" in e.tool
            for e in task.scenario.events
        )
        initial = evidence.frames.get(evidence.source.get("initial_frame"), {})
        final = evidence.frames.get(evidence.source.get("final_frame"), {})
        self.runtime = SimpleNamespace(
            state_recorder=SimpleNamespace(
                initial=frame(initial, "initial"), final=frame(final, "final")
            )
        )
        for index, checks in matching["checks"].items():
            for check in checks:
                if (
                    check.get("reason") == "bound_semantic_parameter_requires_review"
                    and check.get("passed") is None
                ):
                    try:
                        check["passed"] = self.semantic(
                            task, refs[index], check, judge, user
                        )
                        check["reason"] = "native_bound_semantic_group"
                    except Exception as exc:
                        check["reason"] = "semantic_unavailable:" + type(exc).__name__
        effects = (
            EffectEvaluator(self, refs, actual, matching).evaluate()
            if refs
            else self.query(task, evidence, judge)
        )
        dimensions = {
            "tools": tool_score(refs, actual, matching)
            if refs
            else dimension([], "query_uses_binary_business_result"),
            "parameters": parameter_score(refs, actual, matching, self.user_name),
            "order": order_score(refs, actual, edges, matching),
            "effects": effects,
        }
        total = aggregate(dimensions)
        reason = evidence.source.get("termination_reason")
        diagnostic_only = reason in {
            "infrastructure_error",
            "user_error",
            "unexpected_error",
        }
        if diagnostic_only:
            total.update(
                score=None,
                lower=None,
                upper=None,
                reason="invalid_trial_diagnostic_only",
            )
        return {
            "version": "tau-real-execution/1",
            "status": "measured"
            if total["score"] is not None
            else "evidence_incomplete",
            "dimensions": dimensions,
            "total": total,
            "matching": matching,
            "diagnostic_only": diagnostic_only,
            "weights": {"tools": 25, "parameters": 35, "order": 10, "effects": 30},
            "reference_count": len(refs),
            "actual_count": len(actual),
            "stages": evidence.stages,
        }

    @staticmethod
    def user(evidence):
        for states in evidence.frames.values():
            for name in ("Contacts", "InternalContacts"):
                for contact in states.get(name, {}).get("contacts", {}).values():
                    if contact.get("is_user"):
                        return contact
        return {}

    def app_name(self, operation):
        return operation["app"]

    def phase_frames(self, index):
        op = self.actual[index]["evidence"]
        return frame(self.evidence.frames.get(op.before_frame), op.before_frame), frame(
            self.evidence.frames.get(op.after_frame), op.after_frame
        )

    @staticmethod
    def semantic(task, reference, row, judge, user):
        from tau2.diagnostics.execution.semantic import native_compare

        return native_compare(task, reference, row, judge, user)

    @staticmethod
    def query(task, evidence, judge):
        units = []
        for stage in task.scenario.stages:
            expected = stage.criteria.get("communicate_info") or []
            if not expected:
                continue
            stage_replies = [
                r
                for r in evidence.user_inputs
                if r.role == "assistant_context" and r.stage == stage.stage
            ]
            missing_transcript = any(r.unavailable_reason for r in stage_replies)
            messages = [
                r.text
                for r in stage_replies
                if r.complete and not r.unavailable_reason and r.text
            ]
            for fact in expected:
                if not messages:
                    passed, reason = (
                        (None, "delivered_answer_transcript_unavailable")
                        if missing_transcript
                        else (False, "required_answer_not_delivered")
                    )
                else:
                    try:
                        row = {
                            "parameter": "$semantic_group",
                            "fields": ["content"],
                            "actual": {"content": "\n".join(messages)},
                            "expected": {"content": fact},
                        }
                        passed = Gaia2ExecutionAdapter.semantic(
                            task,
                            {
                                "tool": "AgentUserInterface__send_message_to_user",
                                "stage": stage.stage,
                            },
                            row,
                            judge,
                            Gaia2ExecutionAdapter.user(evidence),
                        )
                        reason = "native_are_answer_checker"
                        if passed is False and missing_transcript:
                            passed, reason = (
                                None,
                                "delivered_answer_transcript_unavailable",
                            )
                    except Exception as exc:
                        passed, reason = None, type(exc).__name__
                units.append({"stage": stage.stage, "passed": passed, "reason": reason})
        if not units:
            return dimension([], "no_required_query_answer")
        values = [row["passed"] for row in units]
        passed = False if False in values else None if None in values else True
        return dimension(
            [{"passed": passed, "reason": "whole_task_query_answer", "checks": units}]
        )

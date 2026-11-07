"""Actual-mail triggers for scripted replies, independent of oracle progress."""

from __future__ import annotations

import copy
import re
from concurrent.futures import ThreadPoolExecutor

from tau2.environment.scenario import equivalent_arguments, resolve_references

REPLY_VERSION = "gaia2-actual-mail-replies-v1"
_REFERENCE = re.compile(r"\{\{([^{}]+)\}\}")
MAIL_TOOLS = {"Emails__send_email", "Emails__reply_to_email"}


class ActualMailReplies:
    """Bind each scripted correspondent to the earliest qualifying sent email."""

    def __init__(self, runtime, lookup):
        self.runtime = runtime
        self.lookup = lookup
        events = {e.id: e for e in runtime.spec.events}
        self.rules = {}
        self.stage_user_ids = {
            next(events[k].id for k in stage.event_ids if events[k].kind == "user")
            for stage in runtime.spec.stages
        }
        for event in events.values():
            match = _REFERENCE.fullmatch(str(event.arguments.get("email_id", "")))
            if (
                event.kind != "environment"
                or event.tool != "Emails__reply_to_email_from_user"
                or not match
            ):
                continue
            origin = events[match[1]]
            if origin.tool not in MAIL_TOOLS:
                continue
            stage = next(
                (s for s in runtime.spec.stages if origin.id in s.event_ids), None
            )
            user = (
                next(
                    (events[k].id for k in stage.event_ids if events[k].kind == "user"),
                    None,
                )
                if stage
                else next(
                    (
                        e.id
                        for e in events.values()
                        if e.kind == "user" and not e.dependencies
                    ),
                    None,
                )
            )
            self.rules[event.id] = (event, origin, user)
        self.managed = set(self.rules)
        self.candidates = []
        self.bindings = {}
        self.matches = {}

    def observe(self, name, arguments, result, when, call_id):
        """Capture a successful outgoing email, without consuming oracle observations."""
        if name not in MAIL_TOOLS or not isinstance(result, str):
            return
        if any(c["call_id"] == call_id for c in self.candidates):
            return
        mail = self.lookup(result)
        self.candidates.append(
            {"call_id": call_id, "mail_id": result, "mail": mail, "time": when}
        )

    def _matches_text(self, actual, expected):
        if equivalent_arguments(actual, expected):
            return True
        if self.runtime.semantic_matcher is None:
            return False
        key = (actual, expected)
        if key not in self.matches:
            if self.runtime._match_executor is None:
                self.runtime._match_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="tau-evidence"
                )
            self.matches[key] = self.runtime._match_executor.submit(
                self.runtime.semantic_matcher, actual, expected
            )
        future = self.matches[key]
        return future.result() if future.done() else None

    def advance(self):
        """Resolve nonblocking matches and journal immutable per-reply bindings."""
        rt = self.runtime
        for key, (event, origin, user) in self.rules.items():
            if key in self.bindings or key in rt.completed or user not in rt.completed:
                continue
            try:
                expected = resolve_references(origin.arguments, rt.results)
            except KeyError:
                continue
            lower = rt.completed[user]
            later = [
                rt.completed[e.id]
                for e in rt.spec.events
                if e.kind == "user"
                and e.id != user
                and e.id in rt.completed
                and rt.completed[e.id] > lower
                and e.id in self.stage_user_ids
            ]
            upper = min(later, default=float("inf"))
            for candidate in self.candidates:
                if not lower <= candidate["time"] < upper:
                    continue
                mail = candidate["mail"]
                if event.arguments["sender"].casefold() not in {
                    str(x).casefold() for x in mail.get("recipients", [])
                }:
                    continue
                if (
                    origin.tool == "Emails__reply_to_email"
                    and mail.get("parent_id") != expected["email_id"]
                ):
                    continue
                # The message body describes this business interaction; optional
                # calendar fields, CCs and preceding oracle operations do not gate it.
                actual_text = str(mail.get("content", ""))
                expected_text = str(expected.get("content", ""))
                matched = self._matches_text(actual_text, expected_text)
                if matched is None:
                    break  # Do not let a later candidate overtake an unresolved one.
                if not matched:
                    continue
                binding = {
                    "version": REPLY_VERSION,
                    "mail_id": candidate["mail_id"],
                    "call_id": candidate["call_id"],
                    "sent_time": rt.spec.start_time + candidate["time"],
                    "matched_time": rt.time(),
                }
                self.bindings[key] = binding
                rt.record(
                    "trigger",
                    event_id=key,
                    name="actual_mail_reply",
                    arguments={"origin_event_id": origin.id},
                    result=copy.deepcopy(binding),
                )
                break

    def ready(self, event):
        """Return whether a managed reply has a real-mail binding."""
        return event.id in self.bindings

    def due(self, event):
        """Use actual send completion as the delay origin."""
        return (
            self.bindings[event.id]["sent_time"]
            - self.runtime.spec.start_time
            + event.delay_seconds
        )

    def arguments(self, event):
        """Resolve the reply target without forging an oracle ID mapping."""
        args = copy.deepcopy(event.arguments)
        args["email_id"] = self.bindings[event.id]["mail_id"]
        return resolve_references(args, self.runtime.results)


def reply_audit(tasks):
    """Inventory migrated and retained environment dependencies for review."""
    rows = []
    for task in tasks:
        events = {e.id: e for e in task.scenario.events}
        for event in events.values():
            if event.kind != "environment":
                continue
            match = _REFERENCE.fullmatch(str(event.arguments.get("email_id", "")))
            migrated = (
                event.tool == "Emails__reply_to_email_from_user"
                and match
                and events[match[1]].tool in MAIL_TOOLS
            )
            rows.append(
                {
                    "task_id": task.id,
                    "event_id": event.id,
                    "tool": event.tool,
                    "rule": "actual_mail" if migrated else "retained_reference_graph",
                    "dependencies": [
                        {
                            "id": k,
                            "kind": events[k].kind,
                            "tool": events[k].tool,
                            "channel": events[k].channel,
                        }
                        for k in event.dependencies
                    ],
                }
            )
    return {
        "version": REPLY_VERSION,
        "migrated_events": sum(r["rule"] == "actual_mail" for r in rows),
        "migrated_tasks": len(
            {r["task_id"] for r in rows if r["rule"] == "actual_mail"}
        ),
        "events": rows,
    }

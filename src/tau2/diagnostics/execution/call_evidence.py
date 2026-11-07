"""Associate external calls with ARE root events without hiding extra writes."""

from __future__ import annotations

import math

from .checks import canonical_tool, event_args

AUI = "AgentUserInterface__send_message_to_user"
# Compatibility for the pinned ARE implementation, not a general extra-READ waiver.
LEGACY_READ_CHILDREN = {
    "CalendarApp__read_today_calendar_events": {
        "CalendarApp__get_calendar_events_from_to"
    }
}
# Named parameters of the pinned ARE SandboxLocalFileSystem **kwargs methods.
# The remaining externally supplied keys are packed into the recorded kwargs.
VAR_KEYWORD_SIGNATURES = {
    "SandboxLocalFileSystem__open": {
        "path",
        "mode",
        "block_size",
        "cache_options",
        "compression",
    },
    "SandboxLocalFileSystem__cat": {"path", "recursive", "on_error"},
    "SandboxLocalFileSystem__mkdir": {"path", "create_parents"},
    "SandboxLocalFileSystem__mv": {"path1", "path2", "recursive", "maxdepth"},
    "SandboxLocalFileSystem__ls": {"path", "detail"},
    "SandboxLocalFileSystem__exists": {"path"},
    "SandboxLocalFileSystem__info": {"path"},
}


def direct_arguments_agree(recorded, supplied):
    return all(
        key in recorded and recorded[key] == value for key, value in supplied.items()
    )


def arguments_agree(tool, recorded, supplied):
    """Compare actual Python keyword binding, without dropping or flattening values."""
    named = VAR_KEYWORD_SIGNATURES.get(tool)
    if named is None:
        return direct_arguments_agree(recorded, supplied)
    extra = {key: value for key, value in supplied.items() if key not in named}
    return recorded.get("kwargs", {}) == extra and all(
        key in recorded and recorded[key] == value
        for key, value in supplied.items()
        if key in named
    )


def event_tool(event):
    action = event.get("action") or {}
    return canonical_tool(action.get("app", ""), action.get("function", ""))


def call_tool(call):
    app, separator, function = call.get("tool", "").partition("__")
    if not separator:
        raise ValueError("Invalid external call tool")
    return canonical_tool(app, function)


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def interval(call):
    start, end = call.get("started_sim_time"), call.get("completed_sim_time")
    if finite(start) and finite(end) and start <= end:
        return start, end
    return None


def within(event, call):
    bounds = interval(call)
    stamp = event.get("event_time")
    return bounds is not None and finite(stamp) and bounds[0] <= stamp <= bounds[1]


def agrees(event, call):
    args = event_args(event)
    return event_tool(event) == call_tool(call) and arguments_agree(
        event_tool(event), args, call.get("args") or {}
    )


def align_calls(events: list, calls: list, observed_app_calls=None) -> dict:
    app_events = [
        e for e in events if e.get("event_type") == "AGENT" and event_tool(e) != AUI
    ]
    by_id = {e["event_id"]: e for e in app_events}
    if len(by_id) != len(app_events):
        raise ValueError("Duplicate application event IDs")
    if observed_app_calls is not None and observed_app_calls != len(calls):
        raise ValueError("Official call count disagrees with archived evidence")
    explicit = any("are_event_ids" in c for c in calls)
    timed = bool(calls) and all(interval(c) is not None for c in calls)
    assigned, groups, call_ids = {}, [], set()
    for index, call in enumerate(calls):
        if call.get("call_id") is not None:
            if call["call_id"] in call_ids:
                raise ValueError("Duplicate external call IDs")
            call_ids.add(call["call_id"])
        if explicit:
            identities = call.get("are_event_ids")
            root_id = call.get("are_root_event_id")
            rejection = call.get("argument_binding_rejection")
            if (
                identities == []
                and root_id is None
                and call.get("are_event_capture_complete") is True
                and not call.get("are_event_capture_error")
                and call.get("completed") is True
                and call.get("error") == "TypeError"
                and call.get("failure_stage") == "argument_binding"
                and isinstance(rejection, dict)
                and rejection.get("operation_type") == "READ"
                and rejection.get("signature")
                and rejection.get("binding_error")
                and rejection.get("error_message")
                and interval(call) is not None
            ):
                groups.append(
                    {
                        "call_index": index,
                        "call_id": call.get("call_id"),
                        "root_event_id": None,
                        "event_ids": [],
                        "mode": "read_rejected_before_execution",
                    }
                )
                continue
            if (
                not isinstance(identities, list)
                or not identities
                or len(set(identities)) != len(identities)
                or any(
                    identity not in by_id or identity in assigned
                    for identity in identities
                )
            ):
                raise ValueError("Incomplete or conflicting captured ARE event group")
            recovered = False
            if (
                call.get("are_event_capture_complete") is not True
                or root_id not in identities
            ):
                # v3.0.0 captured the IDs but failed to recognize literal kwargs.
                # Repair only this proved representation mismatch inside that group.
                candidates = [
                    by_id[identity]
                    for identity in identities
                    if agrees(by_id[identity], call)
                    and within(by_id[identity], call)
                    and call_tool(call) in VAR_KEYWORD_SIGNATURES
                    and not direct_arguments_agree(
                        event_args(by_id[identity]), call.get("args") or {}
                    )
                ]
                if (
                    call.get("are_event_capture_complete") is not False
                    or root_id is not None
                    or call.get("are_event_capture_error")
                    or len(candidates) != 1
                ):
                    raise ValueError(
                        "Incomplete or conflicting captured ARE event group"
                    )
                root_id = candidates[0]["event_id"]
                recovered = True
            root = by_id[root_id]
            if not agrees(root, call) or not within(root, call):
                raise ValueError("Captured root event disagrees with external call")
            for identity in identities:
                event = by_id[identity]
                if not within(event, call):
                    raise ValueError("Captured event outside external call interval")
                if (
                    identity != root_id
                    and event.get("action", {}).get("operation_type") != "READ"
                ):
                    raise ValueError("Unmatched internal WRITE or unclassified event")
        elif timed:
            candidates = [
                e
                for e in app_events
                if e["event_id"] not in assigned and agrees(e, call) and within(e, call)
            ]
            if len(candidates) != 1:
                raise ValueError("External call root matching is missing or ambiguous")
            root = candidates[0]
            root_id, identities = root["event_id"], [root["event_id"]]
        else:
            if len(app_events) != len(calls):
                raise ValueError(
                    "Trace/call-log operation counts disagree without timed evidence"
                )
            root = sorted(app_events, key=lambda e: e.get("event_time") or 0)[index]
            if not agrees(root, call):
                raise ValueError("Trace/call-log tools or arguments disagree")
            root_id, identities = root["event_id"], [root["event_id"]]
        for identity in identities:
            assigned[identity] = index
        groups.append(
            {
                "call_index": index,
                "call_id": call.get("call_id"),
                "root_event_id": root_id,
                "event_ids": list(identities),
                "mode": "captured_variadic_binding_recovered"
                if explicit and recovered
                else "captured"
                if explicit
                else "legacy_timed"
                if timed
                else "legacy_sequence",
            }
        )
    for identity, event in by_id.items():
        if identity in assigned:
            continue
        if explicit or event.get("action", {}).get("operation_type") != "READ":
            raise ValueError("Unmatched application WRITE or unexplained event")
        parents = [
            g
            for g in groups
            if event_tool(event)
            in LEGACY_READ_CHILDREN.get(call_tool(calls[g["call_index"]]), set())
            and within(event, calls[g["call_index"]])
            and by_id[g["root_event_id"]].get("action", {}).get("operation_type")
            == "READ"
        ]
        if len(parents) != 1:
            raise ValueError("Unexplained or ambiguous internal READ event")
        parent = parents[0]
        parent["event_ids"].append(identity)
        assigned[identity] = parent["call_index"]
    return {
        "groups": groups,
        "root_event_to_call_index": {
            g["root_event_id"]: g["call_index"]
            for g in groups
            if g["root_event_id"] is not None
        },
        "internal_read_event_ids": [
            identity
            for g in groups
            for identity in g["event_ids"]
            if identity != g["root_event_id"]
        ],
    }

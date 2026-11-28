"""Normalize recorded causal order without comparing independent clocks."""

from copy import deepcopy

from tau2.diagnostics.state.capture import CAPTURE_VERSION
from tau2.diagnostics.state.models import DialogueEvent

EVENT_VERSION = "tau-state-events/2"


def instruction_fields(task):
    """Read private initial context only for legacy callers, not scoring prompts."""
    value = getattr(getattr(task, "user_scenario", None), "instructions", None)
    return (
        value.model_dump(mode="json")
        if hasattr(value, "model_dump")
        else {"task_instructions": str(value or "")}
    )


def build_events(task, evidence):
    """Use recorded sequence for text, one local monotonic clock for media."""
    if evidence.state_events:
        return [DialogueEvent.model_validate(row) for row in evidence.state_events]
    # Old evidence cannot relate dialogue indices and tool runtime seconds.
    # Preserve only the known dialogue order. Do not attach late tool results.
    return [
        DialogueEvent(
            event_id=r.id,
            kind="user_turn" if r.role == "user" else "agent_turn",
            role="user" if r.role == "user" else "agent",
            sequence=i,
            content=r.text,
            visible_to=["agent"],
            source="legacy_dialogue",
            metadata={"incomplete": bool(r.unavailable_reason) or not r.complete},
        )
        for i, r in enumerate(
            sorted(evidence.user_inputs, key=lambda r: (r.started, r.id))
        )
        if r.text
    ]


def recorded_events(simulation, evidence, operation_events):
    """Build text from full messages; voice from delivery clocks and call IDs."""
    result = []

    def add(id, kind, role, content, visible, at=None, **metadata):
        result.append(
            dict(
                event_id=id,
                kind=kind,
                role=role,
                content=content,
                visible_to=visible,
                timestamp=at,
                source=EVENT_VERSION,
                sequence=len(result),
                metadata=metadata,
            )
        )

    operations = {op.call_id: op for op in evidence.operations}
    if not simulation.ticks:
        for i, message in enumerate(getattr(simulation, "messages", [])):
            if message.role in ("user", "assistant"):
                if message.content and not any(
                    s in message.content
                    for s in ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")
                ):
                    add(
                        f"message:{i}",
                        "user_turn" if message.role == "user" else "agent_turn",
                        "user" if message.role == "user" else "agent",
                        message.content,
                        ["agent"],
                    )
                for call in (
                    (getattr(message, "tool_calls", None) or [])
                    if message.role == "assistant"
                    else []
                ):
                    add(
                        "request:" + call.id,
                        "tool_requested",
                        "agent",
                        dict(call_id=call.id, name=call.name, arguments=call.arguments),
                        ["agent"],
                    )
            elif message.role == "tool" and message.requestor == "assistant":
                op = operations.get(message.id)
                content = dict(
                    call_id=message.id,
                    name=op.name if op else "",
                    result=op.result if op else message.content,
                    error=op.error if op else message.error,
                )
                add(
                    "completed:" + message.id,
                    "tool_completed",
                    "environment",
                    content,
                    [],
                )
                add(
                    "delivered:" + message.id,
                    "tool_delivered",
                    "agent",
                    content,
                    ["agent"],
                )
        return result
    # The recorder also carries complete caller utterances and confirmed ASR.
    # Legacy dialogue hooks only recorded final chunks, so reconstruct those
    # from delivered_inputs instead of treating a final chunk as a whole turn.
    captured_dialogue = set()
    for row in operation_events:
        if row["kind"] in {"user_turn", "agent_turn"}:
            if (row.get("metadata") or {}).get("capture_version") != CAPTURE_VERSION:
                continue
            captured_dialogue.add(row["kind"])
        # Dialogue content is a string; tool payloads are dictionaries. Copy
        # both without changing their types or mutating the online event log.
        row = deepcopy(row)
        row["sequence"] = len(result)
        if row["kind"] == "tool_delivered":
            op = operations.get(row["content"]["call_id"])
            if op is not None:
                row["content"].update(
                    name=op.name, result=deepcopy(op.result), error=op.error
                )
        result.append(row)
    origin = evidence.audio.get("activity", {}).get("origin")
    if origin is None and not captured_dialogue:
        return []  # no basis for comparing legacy media with runtime seconds
    for row in evidence.user_inputs:
        kind = "user_turn" if row.role == "user" else "agent_turn"
        if kind in captured_dialogue or origin is None:
            continue
        if not row.text and not row.unavailable_reason:
            continue
        user = row.role == "user"
        add(
            row.id,
            kind,
            "user" if user else "frontend",
            row.text,
            ["frontend"],
            origin + (row.ended if user else row.started),
            incomplete=bool(row.unavailable_reason) or not row.complete,
            interval=[origin + row.started, origin + row.ended],
        )
    # Backend input logs describe actual accepted delegations, not completed work.
    for observation in evidence.source.get("state_backend_observations", []):
        at = observation.get("received_monotonic")
        if at is None:
            continue
        identity = str(
            observation.get("call_id") or observation.get("event_id") or len(result)
        )
        kind = observation.get("kind")
        if kind == "native_operation_started":
            add(
                "native-request:" + identity,
                "tool_requested",
                "frontend",
                dict(
                    call_id="native:" + identity,
                    name=observation.get("operation") or "task_management",
                    arguments=observation.get("arguments") or {},
                ),
                ["frontend"],
                at,
            )
        elif kind == "native_operation_completed":
            add(
                "native-delivered:" + identity,
                "tool_delivered",
                "frontend",
                dict(
                    call_id="native:" + identity,
                    result=observation.get("result"),
                    error=observation.get("success") is False,
                ),
                ["frontend"],
                at,
            )
        else:
            # Passive lifecycle observation is evaluator evidence, not proof of
            # frontend notification. It cannot ground a frontend claim.
            add(
                "observation:" + identity,
                "backend_observation",
                "backend",
                observation,
                [],
                at,
            )
    for row in evidence.backend_inputs:
        if row.internal or row.accepted is not True:
            continue
        # No clock mapping from an independent provider clock is assumed.
        at = (
            row.content.get("received_monotonic")
            if isinstance(row.content, dict)
            else None
        )
        if at is not None:
            add(
                "backend:" + row.id,
                "backend_observation",
                "backend",
                row.content,
                ["backend"],
                at,
            )
    # Equal timestamps: behaviors before completions/deliveries, conservatively.
    priority = {
        "tool_requested": 0,
        "agent_turn": 0,
        "tool_completed": 1,
        "tool_delivered": 2,
        "user_turn": 2,
    }
    result.sort(
        key=lambda r: (r["timestamp"], priority.get(r["kind"], 1), r["sequence"])
    )
    for i, row in enumerate(result):
        row["sequence"] = i
    return result

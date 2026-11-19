"""Qwen backend event adapter."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tau2.persistent_tasks.core.models import BackendTaskObservation, CanonicalState
from tau2.persistent_tasks.core.observation import event_from_mapping
from tau2.persistent_tasks.models import ObservedEvent


class QwenEventAdapter:
    """Translate Qwen/ACP payloads without judging business progress."""

    provider = "qwen"

    def __init__(self):
        self.reply_buffers = {}

    def convert(self, payload: Mapping[str, Any]) -> ObservedEvent:
        value = dict(payload)
        status = value.get("status") or value.get("state")
        kind = value.get("kind") or value.get("type") or value.get("event")
        if kind in {"tool_result", "function_call_output"} or status in {
            "completed",
            "succeeded",
            "success",
        }:
            value.setdefault("kind", "tool_call_completed")
            value.setdefault("accepted", True)
        elif status in {"failed", "error", "cancelled"}:
            value.setdefault("kind", "tool_call_failed")
            value.setdefault("accepted", False)
        return event_from_mapping(value, provider=self.provider)

    def events(self, payloads: Iterable[Mapping[str, Any]]) -> list[ObservedEvent]:
        return [self.convert(payload) for payload in payloads]

    def backend_observation(self, payload: Mapping[str, Any]) -> BackendTaskObservation:
        """Translate native lifecycle and call evidence to canonical semantics."""
        value = dict(payload)
        session = value.get("session_id")
        task = value.get("task") or {}
        args = dict(value.get("arguments") or {})
        discovery = args.pop("list_all", False) is True
        args.pop("task_id", None)
        status = value.get("state") or value.get("status")
        canonical = QWEN_STATES.get(status)
        if status is not None and status != "cancelling" and canonical is None:
            canonical = CanonicalState.UNKNOWN
        result = value.get("result")
        kind = value.get("kind") or value.get("type") or "backend_update"
        if kind.startswith("native_operation_"):
            result = normalize_result(result, session)
        updates = [
            {"instruction": update.get("instruction"), "effect": "delivered"}
            for update in task.get("updates", [])
            if update.get("status") == "delivered"
        ]
        return BackendTaskObservation(
            provider=self.provider,
            kind=kind,
            timestamp=float(value.get("timestamp", value.get("time", 0))),
            backend_task_id=scoped_id(
                session, value.get("task_id") or value.get("backend_task_id")
            ),
            source_task_id=value.get("source_task_id"),
            source_stage=value.get("source_stage"),
            status=canonical.value if canonical else None,
            operation=value.get("operation"),
            arguments=args,
            result=result,
            raw=value,
            objective=task.get("objective"),
            purpose="task_discovery" if discovery else None,
            effect=(result or {}).get("effect") if isinstance(result, dict) else None,
            confirmed_updates=updates,
            event_id=value.get("event_id"),
            sequence=value.get("sequence"),
            session_id=session,
            call_id=scoped_id(session, value.get("call_id")),
            parent_task_id=scoped_id(session, value.get("parent_task_id")),
            success=value.get("success"),
            result_available=value.get("result_available", False),
            received_monotonic=value.get("received_monotonic"),
            provider_timestamp=value.get("provider_timestamp"),
        )

    def normalize_event(self, payload):
        """Return zero or one canonical observation, without executing tools."""
        value = dict(payload)
        event = value.get("event") or {}
        if value.get("type") == "gateway_event":
            if event.get("role") not in (None, "assistant", "agent"):
                return []
            kind = event.get("type", "")
            if kind not in {
                "transcript.delta",
                "transcript.final",
                "response.audio_transcript.delta",
                "response.audio_transcript.done",
                "response.output_audio_transcript.delta",
                "response.output_audio_transcript.done",
                "response.created",
                "response.started",
            }:
                return []
            response = (
                event.get("responseId")
                or event.get("response_id")
                or (event.get("response") or {}).get("id")
            )
            if not response:
                return []
            from tau2.utils.transcript.generated import sanitize_generated_text

            response_key = scoped_id(value.get("session_id"), response)
            buffer = self.reply_buffers.setdefault(
                response_key, {"text": "", "final": False}
            )
            text = str(
                event.get("content")
                or event.get("transcript")
                or event.get("delta")
                or ""
            )
            final = kind.endswith(("final", "done"))
            if final:
                buffer.update(text=text or buffer["text"], final=True)
            elif not buffer["final"]:
                buffer["text"] += text
            safe_text = buffer["text"]
            if "<DSML" in safe_text and "</DSML calls>" not in safe_text:
                safe_text = safe_text.split("<DSML", 1)[0]
            return [
                BackendTaskObservation(
                    provider=self.provider,
                    kind="generated_reply",
                    timestamp=0,
                    session_id=value.get("session_id"),
                    response_id=response_key,
                    turn_id=event.get("turnId") or event.get("turn_id"),
                    text=sanitize_generated_text(safe_text),
                    final=final,
                    text_is_snapshot=True,
                    received_monotonic=value.get("received_monotonic"),
                )
            ]
        if value.get("type") == "diagnostic_backend":
            return []
        return [self.backend_observation(value)]


def scoped_id(session, identifier):
    """Keep provider identifiers unique across sessions."""
    if identifier is None or session is None:
        return identifier
    import json

    return json.dumps([session, str(identifier)], separators=(",", ":"))


def normalize_result(result, session=None):
    """Exclude tool instructions and preserve business evidence and acknowledgements."""
    if not isinstance(result, dict):
        return {
            "state": None,
            "result": result,
            "result_available": False,
            "accepted": False,
            "effect": None,
            "error": None,
        }
    status = result.get("status")
    state = QWEN_STATES.get(result.get("task_status"))
    return {
        "state": state.value if state else None,
        "result": result.get("result"),
        "result_available": state == CanonicalState.COMPLETED and "result" in result,
        "error": result.get("error"),
        "accepted": status == "accepted",
        "effect": "cancelled" if status == "cancelled" else None,
        "progress": {
            key: result.get(key)
            for key in (
                "objective",
                "elapsed_ms",
                "authorization_pending",
                "latest_update",
                "recent_updates",
                "artifacts",
            )
            if key in result
        },
        "tasks": [
            {
                "backend_task_id": scoped_id(session, task.get("task_id")),
                "state": (
                    QWEN_STATES.get(task.get("status")) or CanonicalState.UNKNOWN
                ).value,
                "objective": task.get("objective"),
            }
            for task in result.get("tasks", [])
        ]
        if isinstance(result.get("tasks"), list)
        else None,
    }


QWEN_STATES = {
    "scheduled": CanonicalState.QUEUED,
    "queued": CanonicalState.QUEUED,
    "pending": CanonicalState.QUEUED,
    "running": CanonicalState.RUNNING,
    "started": CanonicalState.RUNNING,
    "delegated": CanonicalState.RUNNING,
    "finalizing": CanonicalState.RUNNING,
    "waiting": CanonicalState.WAITING,
    "blocked": CanonicalState.WAITING,
    "completed": CanonicalState.COMPLETED,
    "succeeded": CanonicalState.COMPLETED,
    "failed": CanonicalState.FAILED,
    "error": CanonicalState.FAILED,
    "cancelled": CanonicalState.CANCELLED,
    "canceled": CanonicalState.CANCELLED,
}

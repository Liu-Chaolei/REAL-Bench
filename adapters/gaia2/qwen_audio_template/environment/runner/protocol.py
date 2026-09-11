"""Trusted voice-variant projection. No reference-dependent classification."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "gaia2-qwen-audio/3"
KINDS = {"progress", "checkpoint", "final", "clarification", "failure"}
STATES = {"running", "waiting_environment", "waiting_user", "completed", "failed"}
TERMINAL = {"waiting_user", "completed", "failed"}


class ProtocolError(RuntimeError):
    """A model violated the interaction contract; counts as a failed trial."""


@dataclass
class Delivery:
    request_id: str
    delivery_id: str
    kind: str
    response_id: str | None = None
    transcript: str | None = None
    requested_content: str | None = None
    played: bool = False
    cancelled: bool = False
    committed: bool = False
    status: str = "queued"
    declared_kind: str | None = None
    work_id: str | None = None
    attempt_id: str = "1"
    playback_end_sec: float | None = None


class Protocol:
    version = VERSION

    def __init__(self, output: Path, clock, request_id="gaia2-request"):
        self.output = output
        self.clock = clock
        self.request_id = request_id
        self.state = "running"
        self.closed = False
        self.awaiting_environment = False
        self.deliveries: dict[str, Delivery] = {}
        self.responses: dict[str, str] = {}
        self.notifications: dict[str, dict[str, Any]] = {}
        self.consumed_notifications: set[str] = set()
        self.created_at = time.monotonic()
        self.started_at: float | None = None
        self.epoch: float | None = None
        self.first_action: float | None = None
        self.last_delivery: float | None = None
        self.final_delivery: float | None = None
        self.turn = 0
        self.sequence = 0
        self.lock = asyncio.Lock()
        self.errors: list[str] = []
        output.mkdir(parents=True, exist_ok=True)

    def record(self, event, **payload):
        self.sequence += 1
        row = {
            "protocol": self.version,
            "sequence": self.sequence,
            "event": event,
            "request_id": self.request_id,
            "wall_time": time.time(),
            "elapsed_sec": self.elapsed(),
            **payload,
        }
        with (self.output / "protocol.jsonl").open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def start(self):
        if self.started_at is not None:
            raise ProtocolError("Input clock already started")
        self.started_at = time.monotonic()
        self.epoch = self.clock()[0]
        self.record("input.started")

    def elapsed(self):
        return None if self.epoch is None else max(0, self.clock()[0] - self.epoch)

    def declare(self, delivery_id, kind, request_id=None):
        if request_id is not None and request_id != self.request_id:
            raise ProtocolError("Delivery belongs to another request")
        if not isinstance(delivery_id, str) or not delivery_id or kind not in KINDS:
            raise ProtocolError("Invalid delivery identity or kind")
        existing = self.deliveries.get(delivery_id)
        if existing:
            if existing.declared_kind != kind:
                raise ProtocolError("Cannot reclassify an existing delivery")
            return existing
        delivery = Delivery(self.request_id, delivery_id, kind, declared_kind=kind)
        self.deliveries[delivery_id] = delivery
        self.record("delivery.declared", delivery_id=delivery_id, kind=kind)
        return delivery

    def bind(self, response_id, delivery_id, kind):
        delivery = self.declare(delivery_id, kind)
        previous = self.responses.get(response_id)
        if previous and previous != delivery_id:
            raise ProtocolError("Response already belongs to another delivery")
        if delivery.response_id and delivery.response_id != response_id:
            raise ProtocolError(
                "Delivery already has a response; use a new ID after interruption"
            )
        delivery.response_id = response_id
        self.responses[response_id] = delivery_id
        return delivery

    def assess(self, delivery, kind, evidence):
        if kind not in KINDS or delivery.committed:
            raise ValueError("Invalid assessment transition")
        delivery.kind = kind
        self.record(
            "delivery.assessed",
            delivery_id=delivery.delivery_id,
            declared_kind=delivery.declared_kind,
            assessed_kind=kind,
            evidence=evidence,
        )

    def delivery_status(self, identity):
        delivery = self.deliveries.get(identity)
        if delivery is None:
            raise ProtocolError("Unknown delivery ID")
        return {
            "request_id": self.request_id,
            "delivery_id": identity,
            "response_id": delivery.response_id,
            "work_id": delivery.work_id,
            "attempt_id": delivery.attempt_id,
            "status": delivery.status,
            "played": delivery.played,
            "committed": delivery.committed,
        }

    def set_state(self, state):
        if state not in STATES:
            raise ProtocolError("Invalid request state")
        if self.closed and state != self.state:
            raise ProtocolError("Cannot reopen a terminal request")
        self.awaiting_environment = state == "waiting_environment"
        if state != self.state:
            self.state = state
            self.record("request.state", state=state)

    def suggest_state(self, state):
        # A spoken turn cannot clear an explicitly registered environment wait.
        if not self.awaiting_environment:
            self.set_state(state)

    def action(self, name, successful, *, application=True, started_sec=None):
        if successful and application and self.first_action is None:
            self.first_action = self.elapsed() if started_sec is None else started_sec
        self.record(
            "application.result",
            tool=name,
            successful=successful,
            application=application,
        )

    def notify(self, message):
        identity = message["id"]
        if identity in self.notifications:
            if self.notifications[identity] != message:
                raise ProtocolError("Notification ID reused with different content")
            return False
        self.notifications[identity] = message
        self.record("notification.received", notification=message)
        return True

    def take_notifications(self):
        result = [
            value
            for key, value in self.notifications.items()
            if key not in self.consumed_notifications
        ]
        self.consumed_notifications.update(item["id"] for item in result)
        for message in result:
            self.record(
                "notification.consumed",
                notification_id=message["id"],
                consumer="backend",
            )
        return result

    async def commit(self, delivery, transcript, reply, barrier):
        if delivery.committed:
            if delivery.transcript != transcript:
                raise ProtocolError("Duplicate delivery changed its transcript")
            return False
        if delivery.cancelled or not delivery.played:
            return False
        if not isinstance(transcript, str) or not transcript.strip():
            raise ProtocolError("Formal delivery has no confirmed transcript")
        async with self.lock:
            if delivery.committed:
                if delivery.transcript != transcript:
                    raise ProtocolError("Duplicate delivery changed its transcript")
                return False
            delivery.transcript = transcript
            if delivery.kind != "progress":
                # barrier drains previously dispatched application operations and
                # synchronously processes ARE's normal turn check before releasing.
                await barrier(reply, transcript)
                self.turn += 1
                self.last_delivery = (
                    delivery.playback_end_sec
                    if delivery.playback_end_sec is not None
                    else self.elapsed()
                )
                if delivery.kind in {"final", "clarification", "failure"}:
                    self.final_delivery = self.last_delivery
            delivery.committed = True
            delivery.status = "committed"
            self.record(
                "delivery.committed",
                delivery_id=delivery.delivery_id,
                response_id=delivery.response_id,
                work_id=delivery.work_id,
                kind=delivery.kind,
                transcript=transcript,
                are_turn=self.turn if delivery.kind != "progress" else None,
                playback_end_sec=delivery.playback_end_sec,
                are_commit_sec=self.elapsed() if delivery.kind != "progress" else None,
                attempt_id=delivery.attempt_id,
            )
            return True

    def can_finish(self, *, input_done, idle):
        pending = any(
            not d.committed and not d.cancelled for d in self.deliveries.values()
        )
        ready = (
            input_done
            and idle
            and not pending
            and not self.awaiting_environment
            and self.state in TERMINAL
        )
        if ready:
            self.closed = True
        return ready

    def metrics(self):
        raw = None if self.started_at is None else time.monotonic() - self.started_at
        elapsed = self.elapsed()
        return {
            "runner_setup_sec": None
            if self.started_at is None
            else self.started_at - self.created_at,
            "first_application_action_sec": self.first_action,
            "final_formal_delivery_sec": self.final_delivery,
            "last_formal_delivery_sec": self.last_delivery,
            "elapsed_sec": elapsed,
            "raw_wall_sec": raw,
            "excluded_pause_sec": None if raw is None else max(0, raw - (elapsed or 0)),
            "request_state": self.state,
            "formal_deliveries": self.turn,
        }

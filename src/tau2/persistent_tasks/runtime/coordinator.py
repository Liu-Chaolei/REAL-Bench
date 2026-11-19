"""Live coordination and deterministic replay of native backend evidence."""

from __future__ import annotations

from dataclasses import asdict

from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.core.identity import TaskIdentityBinder
from tau2.persistent_tasks.core.models import (
    BackendRequest,
    BackendTaskObservation,
)
from tau2.persistent_tasks.core.projection import BackendTaskProjector
from tau2.persistent_tasks.runtime.evidence import evidence_digest
from tau2.persistent_tasks.runtime.operation_attribution import OperationAttributor
from tau2.persistent_tasks.runtime.windows import ReplyWindowManager
from tau2.persistent_tasks.triggers.scheduler import BackendTriggerScheduler


class PersistentTaskCoordinator:
    """Own private bindings, releases and operation attribution in one thread."""

    def __init__(self, config: PersistentTaskConfig):
        self.config = config
        self.binder = TaskIdentityBinder([b.model_dump() for b in config.bindings])
        self.projector = BackendTaskProjector()
        by_role = {b.role: b for b in config.bindings}
        self.scheduler = BackendTriggerScheduler(
            [
                BackendRequest(
                    r.request_id,
                    r.text,
                    by_role[r.role].source_task_id,
                    by_role[r.role].source_stage,
                    r.state,
                    r.delay_seconds,
                    True,
                    r.role,
                    r.max_wait_seconds,
                    r.operation,
                    r.reply_expectation,
                    r.expected_arguments,
                    r.request_operation_policy,
                    r.prefetch_max_age_seconds,
                )
                for r in config.requests
            ]
        )
        self.journal: list[dict] = []
        self.transitions: list[dict] = []
        self.attributor = OperationAttributor()
        self.pending: list[BackendTaskObservation] = []
        self.active_request: str | None = None
        self.issues: list[dict] = []
        self.audio_diagnostics: list[dict] = []
        self.seen: set[tuple] = set()
        self.closed_at: float | None = None
        self.windows = ReplyWindowManager(
            self.config, self._record, self.add_audio_diagnostic
        )
        self.capabilities: dict = {}
        self.channel_status: dict = {}
        self.generated_responses: dict[str, dict] = {}
        self.confirmed_updates: list[dict] = []
        self.stage_end: float | None = None

    @property
    def operations(self):
        """Compatibility view of operations owned by the attributor."""
        return self.attributor.operations

    @property
    def prefetch_operation_ids(self):
        """Compatibility view of the attributor's prefetch cache."""
        return self.attributor.prefetch_operation_ids

    @property
    def reply_fragments(self):
        return self.windows.reply_fragments

    @property
    def generated_fragments(self):
        return self.windows.generated_fragments

    @property
    def played_audio_segments(self):
        return self.windows.played_audio_segments

    @property
    def played_replies(self):
        return self.windows.played_replies

    def _record(self, kind, **data):
        self.journal.append({"kind": kind, **data})

    def record_capabilities(self, capabilities):
        """Seal the Adapter's declaration independently of actual observations."""
        self.capabilities = dict(capabilities)
        self._record("capabilities", capabilities=self.capabilities)

    def record_channel_status(self, status):
        """Preserve observer readiness, gaps and shutdown evidence."""
        self.channel_status = dict(status)
        self._record("channel_status", status=self.channel_status)

    def source_event_completed(self, event_id, _result, timestamp):
        """Activate declared source anchors using observed native business effects."""
        self._record("source_event", event_id=event_id, timestamp=timestamp)
        self.binder.completed_events.add(event_id)
        self._bind_pending()

    def delivery_started(self, event_id, timestamp):
        """Start request ownership only when its speech reaches the Agent."""
        self._record("delivery_started", event_id=event_id, timestamp=timestamp)
        if event_id in self.scheduler.rows:
            row = self.scheduler.rows[event_id]
            if row["status"] != "queued" or row["delivery_started_at"] is not None:
                self.issues.append(
                    {"reason": "unexpected_delivery", "request_id": event_id}
                )
                return
            row["delivery_started_at"] = timestamp
            self._attribute_prefetch(event_id, timestamp)
            self.active_request = event_id
            return
        # Native stage request IDs are supplied by the orchestrator, not inferred
        # from model text or arbitrary user clarifications.

    def activate_stage(self, stage, timestamp):
        """Activate the actually spoken stage and close earlier attribution."""
        self._record("activate_stage", stage=stage, timestamp=timestamp)
        if self.binder.active_stage is not None and stage != self.binder.active_stage:
            self.active_request = None
            if (
                self.stage_end is None
                and self.binder.active_stage == self.config.bindings[0].source_stage
            ):
                self.stage_end = timestamp
        self.binder.active_stage = stage
        self.binder.active_since = timestamp
        self.scheduler.activated_at.setdefault(stage, timestamp)
        self._bind_pending()

    def delivery_completed(self, request_id, timestamp):
        """Record complete delivery separately from synthesis or queueing."""
        self._record("delivery_completed", request_id=request_id, timestamp=timestamp)
        row = self.scheduler.rows.get(request_id)
        if row is not None and row["delivery_started_at"] is not None:
            row["delivered_at"] = timestamp
            # Keep queued until a response/operation window is complete, so a
            # second request cannot preempt the currently unanswered request.

    def ordinary_user_started(self, timestamp):
        """A new caller turn owns subsequent operations, including permissions."""
        self._record("ordinary_user_started", timestamp=timestamp)
        if self.active_request is not None:
            row = self.scheduler.rows[self.active_request]
            row["window_closed_at"] = timestamp
            row["status"] = (
                "delivered" if row["delivered_at"] is not None else "invalidated"
            )
            self.active_request = None

    def observe(self, observation: BackendTaskObservation):
        """Journal and interpret a deduplicated observation in this runtime."""
        token = (observation.session_id, observation.event_id)
        if observation.event_id and token in self.seen:
            return
        if observation.event_id:
            self.seen.add(token)
        self._record("observation", observation=observation.dump())
        if observation.kind == "generated_reply":
            if not observation.response_id:
                self.issues.append({"reason": "missing_response_identity"})
                return
            row = self.generated_responses.get(observation.response_id)
            if row is None:
                row = {
                    "text": "",
                    "start": observation.timestamp,
                    "end": observation.timestamp,
                    "response_id": observation.response_id,
                    "turn_id": observation.turn_id,
                    "final": False,
                }
                self.generated_responses[observation.response_id] = row
                self.windows.generated_fragments.append(row)
            if observation.final or not row["final"]:
                row["text"] = (
                    (observation.text or "")
                    if observation.text_is_snapshot or observation.final
                    else row["text"] + (observation.text or "")
                )
                row["final"] = observation.final
                row["end"] = observation.timestamp
            return
        for update in observation.confirmed_updates or []:
            self.confirmed_updates.append(
                {
                    "backend_task_id": observation.backend_task_id,
                    "timestamp": observation.timestamp,
                    **update,
                }
            )
        if observation.kind.startswith("native_operation_"):
            self._operation(observation)
            return
        if observation.kind in {
            "task_created",
            "task_state_changed",
            "task_result",
            "backend_task_state",
        }:
            identity = self.binder.bound.get(observation.backend_task_id)
            if (
                identity is not None
                and identity.source_stage != self.binder.active_stage
            ):
                return
            if identity is not None and all(
                self.scheduler.rows[r.request_id]["status"]
                in {"delivered", "expired", "invalidated"}
                for r in self.scheduler.requests
                if r.role == identity.role
            ):
                return
            self.pending.append(observation)
            self._bind_pending()

    def _bind_pending(self):
        open_roles = {
            r.role
            for r in self.scheduler.requests
            if self.scheduler.rows[r.request_id]["status"]
            not in {"delivered", "expired", "invalidated"}
        }
        for observation in self.pending:
            identity = self.binder.bind_observation(
                observation, allowed_roles=open_roles
            )
            if identity:
                self.projector.bind(identity)
        remaining = []
        for observation in self.pending:
            if observation.backend_task_id not in self.projector.identities:
                remaining.append(observation)
                continue
            transition = self.projector.ingest(observation)
            if transition:
                self.transitions.append(
                    {
                        "identity": asdict(transition.identity),
                        "previous": transition.previous.value,
                        "current": transition.current.value,
                        "timestamp": transition.timestamp,
                        "event_id": observation.event_id,
                    }
                )
                self.scheduler.ingest(transition)
        self.pending = remaining

    def _operation(self, event):
        self.attributor.process(
            event,
            active_request=self.active_request,
            scheduler=self.scheduler,
            binder=self.binder,
            projector=self.projector,
            record=self._record,
            issue=self.issues.append,
        )

    def _attribute_prefetch(self, request_id: str, timestamp: float) -> None:
        self.attributor.attribute_prefetch(
            request_id,
            timestamp,
            scheduler=self.scheduler,
            projector=self.projector,
            record=self._record,
        )

    def agent_delivery_seen(self, timestamp):
        """Record playback-time evidence that a response was actually heard."""
        self.windows.add_agent_delivery(timestamp)

    def advance(self, timestamp, boundary):
        """Release ready requests at an observed communication boundary."""
        self._record("advance", timestamp=timestamp, boundary=boundary)
        # A completed spoken request with an operation can close at the next
        # quiet frontend boundary. Semantic correctness is judged after the call.
        if boundary and self.active_request:
            row = self.scheduler.rows[self.active_request]
            if (
                row["delivered_at"] is not None
                and any(t >= row["delivered_at"] for t in self.played_replies)
                and any(
                    o["request_id"] == self.active_request
                    and o["completed_at"] is not None
                    for o in self.operations.values()
                )
            ):
                row["status"] = "delivered"
        return self.scheduler.release(timestamp=timestamp, turn_boundary=boundary)

    def blocks_stage(self, stage):
        """Keep required requests from being skipped by ordinary user STOPs."""
        return any(
            r.source_stage == stage
            and self.scheduler.rows[r.request_id]["status"]
            not in {"delivered", "expired", "invalidated"}
            for r in self.scheduler.requests
        )

    def add_reply(self, text, start, end):
        """Collect finalized ASR at the time of its original audio playback."""
        self.windows.add_reply(text, start, end)

    def add_played_audio(self, start, end):
        """Record measured non-silent playback independently from ASR output."""
        self.windows.add_played_audio(start, end)

    def add_audio_diagnostic(self, reason, **details):
        """Keep playback/ASR diagnostics out of functional evidence failures."""
        diagnostic = {"reason": reason, **details}
        if diagnostic in self.audio_diagnostics:
            return
        self.audio_diagnostics.append(diagnostic)
        self._record(
            "audio_diagnostic",
            diagnostic=diagnostic,
        )

    def add_generated_text(self, text, start, end, *, response_id=None, final=False):
        """Collect provider-generated response text as functional evidence."""
        self.windows.add_generated_text(
            text, start, end, response_id=response_id, final=final
        )

    def summary(self, closed_at):
        """Seal windows, observations and deterministic grading inputs."""
        windows, prefetch_generated_text = self.windows.build(
            self.scheduler.rows, closed_at, self.stage_end
        )
        payload = {
            "version": self.config.version,
            "evidence_version": "persistent-task-evidence-v1",
            "adapter_capabilities": self.capabilities,
            "channel_status": self.channel_status,
            "reply_evidence_version": "persistent-reply-v2",
            "config": self.config.model_dump(mode="json"),
            "config_sha256": self.config.digest(),
            "bindings": [asdict(x) for x in self.binder.bound.values()],
            "transitions": self.transitions,
            "releases": self.scheduler.rows,
            "operations": list(self.operations.values()),
            "confirmed_updates": self.confirmed_updates,
            "reply_windows": windows,
            "audio_diagnostics": self.audio_diagnostics,
            "prefetch_generated_text": prefetch_generated_text,
            "issues": self.issues + self.binder.issues + self.projector.issues,
            "journal": self.journal,
            "closed_at": closed_at,
        }
        payload["evidence_sha256"] = evidence_digest(payload)
        return payload


BackendTaskRuntime = PersistentTaskCoordinator

from tau2.persistent_tasks.runtime.replay import replay  # noqa: E402,F401

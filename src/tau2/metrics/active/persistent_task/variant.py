"""Authored request changes driven by actual deliveries and native task events."""

from dataclasses import replace

from tau2.metrics.active.persistent_task.runtime import PersistentTaskMetricRuntime
from tau2.persistent_tasks.availability import evidence_source


class InteractionVariantController(PersistentTaskMetricRuntime):
    """Drive an authored interaction variant during a live scenario.

    The controller reuses persistent-task evidence plumbing, but its primary
    responsibility is scenario control: it observes backend events and queues
    authored user requests. Its score is a separate post-run evaluation.
    """

    name = "interaction_variant"

    def __init__(self, config, *, variant, **kwargs):
        super().__init__(config, **kwargs)
        self.variant = variant
        self.bindings = {}
        self.binding_status = {}
        self.acceptances = []
        self.requests = []
        self.active_request = None
        self.initial_delivery = None
        self.initial_started = None
        self.states = {}
        self.target_requested = False

    def install(self):
        """Observe the provider without selecting a random intent or fallback."""
        self.source = evidence_source(self.orchestrator.agent)
        if self.source is None or not self.source.get_capabilities().get(
            "task_identity"
        ):
            self.status = "unsupported"
            self.issues.append({"reason": "missing_task_identity_capability"})
            return
        self.capabilities = dict(self.source.get_capabilities())
        if self.variant.variant_type in {
            "modify",
            "cancel",
        } and self.variant.variant_type not in self.capabilities.get("operations", []):
            self.status = "unsupported"
            self.issues.append({"reason": "missing_native_management_operation"})
            return
        self.source.enable()

    def _observe(self, event):
        event = replace(event, timestamp=self._timestamp(event))
        self.observations.append(event.dump())
        if event.kind.startswith("native_operation_"):
            self.operations.append(event.dump())
        if event.backend_task_id and event.status:
            self.states[event.backend_task_id] = event.status
        phase = self._acceptance_role(event)
        if phase is not None and self._is_acceptance(event):
            self._record_acceptance(phase, event)

    def _is_acceptance(self, event):
        """Recognize backend acceptance without semantic matching or judging."""
        if (
            event.backend_task_id is None
            or self.initial_started is None
            or event.timestamp < self.initial_started
        ):
            return False
        if event.kind == "task_created":
            return True
        if event.kind == "task_state_changed" and event.status in {"queued", "running"}:
            return True
        if event.kind != "native_operation_completed":
            return False
        result = event.result if isinstance(event.result, dict) else {}
        return (
            event.operation in {"modify", "addtask"} and result.get("accepted") is True
        )

    def _acceptance_role(self, event):
        """Assign an event to the current request phase by delivery windows."""
        if self.initial_started is None:
            return None
        temporary = next((r for r in self.requests if r["phase"] == "temporary"), None)
        followup = next((r for r in self.requests if r["phase"] == "followup"), None)
        if temporary and temporary["delivery"]["delivered_at"] is not None:
            if followup and followup["delivery"]["delivered_at"] is not None:
                if self._after_delivery(event, followup):
                    return "followup"
            if self._after_delivery(event, temporary):
                return "temporary"
        if followup and followup["delivery"]["delivered_at"] is not None:
            if self._after_delivery(event, followup):
                return "followup"
        return "initial"

    @staticmethod
    def _after_delivery(event, request):
        """Use provider time when present; drain order is authoritative otherwise."""
        if event.received_monotonic is None:
            return True
        return event.timestamp >= request["delivery"]["delivered_at"]

    def _record_acceptance(self, phase, event):
        """Record acceptance and bind only when the backend identity is clear."""
        role = {
            "initial": "primary",
            "temporary": "temporary",
            "followup": "followup",
        }[phase]
        row = {
            "role": role,
            "phase": phase,
            "accepted_at": event.timestamp,
            "kind": event.kind,
            "event_id": event.event_id,
            "call_id": event.call_id,
            "backend_task_id": event.backend_task_id,
        }
        if not any(
            old["role"] == role
            and old.get("backend_task_id") == event.backend_task_id
            and old.get("kind") == event.kind
            for old in self.acceptances
        ):
            self.acceptances.append(row)
        if phase == "initial":
            self._bind_role("primary", event.backend_task_id)
        elif phase == "temporary":
            if event.backend_task_id == self.bindings.get("primary"):
                self.binding_status["temporary"] = "merged"
                row["independent"] = False
            else:
                self._bind_role("temporary", event.backend_task_id)
                row["independent"] = True
        else:
            row["independent"] = event.backend_task_id != self.bindings.get("primary")
        for request in self.requests:
            if request["phase"] == phase:
                request["acceptance"] = row.copy()

    def _bind_role(self, role, backend_task_id):
        existing = self.bindings.get(role)
        if existing is None:
            self.bindings[role] = backend_task_id
            self.binding_status[role] = "bound"
            return
        if existing != backend_task_id:
            self.bindings[role] = None
            self.binding_status[role] = "ambiguous"
            self.issues.append({"reason": "multiple_tasks_for_role", "role": role})

    def _initial_id(self):
        from tau2.orchestrator.scenario_stages import stage_requests

        return stage_requests(self.orchestrator.task.scenario)[self.variant.stage].id

    def _drain(self):
        if self.source is not None:
            for event in self.source.drain_events():
                self._observe(event)

    def _queue(self, phase, text, timestamp):
        request_id = self.variant.request_id(phase)
        delivery = {
            "queued_at": timestamp,
            "delivery_started_at": None,
            "delivered_at": None,
            "reply_started_at": None,
            "reply_completed_at": None,
        }
        self.active_request = dict(
            request_id=request_id,
            phase=phase,
            text=text,
            stage=self.variant.stage,
            delivery=delivery,
            previous_request_id=self._initial_id(),
            operation="addtask" if phase == "temporary" else self.variant.variant_type,
            target_role="temporary"
            if self.variant.variant_type == "cancel"
            else "primary",
        )
        if phase == "followup" and self.variant.variant_type == "cancel":
            self.active_request["previous_request_id"] = self.variant.request_id(
                "temporary"
            )
        self.requests.append(self.active_request)
        self.request_id, self.request_text = request_id, text
        self.delivery = delivery
        runtime = self.orchestrator.environment.scenario_runtime
        queue = getattr(runtime, "queue_user_input", None)
        if queue is None:
            with runtime.lock:
                runtime.user_inputs.append((request_id, text))
        else:
            queue(
                request_id,
                text,
                source="interaction_variant",
                purpose=phase,
            )
        self.status = "queued"

    def advance(self, timestamp, turn_boundary):
        """One authored change, with a separate temporary B only for Cancel."""
        self._drain()
        if self.status == "unsupported" or self.initial_delivery is None:
            return
        if not self.requests:
            if not any(row["phase"] == "initial" for row in self.acceptances):
                return
            if self.variant.variant_type == "cancel":
                self._queue("temporary", self.variant.temporary_request, timestamp)
                return
        if self.target_requested:
            if self.status == "delivered" and self.reply_text and turn_boundary:
                self.delivery["reply_completed_at"] = timestamp
                self.status = "completed"
            return
        role = "temporary" if self.variant.variant_type == "cancel" else "primary"
        if role == "temporary" and not any(
            row["phase"] == "temporary" and row.get("accepted_at") is not None
            for row in self.acceptances
        ):
            return
        target = self.bindings.get(role)
        if role == "temporary" and self.binding_status.get("temporary") == "merged":
            target = None
        if role == "primary" and self.binding_status.get("primary") not in {
            "bound",
            "ambiguous",
        }:
            target = None
        if role == "temporary" and not any(
            row.get("role") == "temporary" and row.get("accepted_at") is not None
            for row in self.acceptances
        ):
            return
        self.backend_task_id = target
        self.source_task_id = self.variant.source_task_id
        self.source_stage = self.variant.stage
        self.task_label = next(
            (
                row["objective"]
                for row in self.observations
                if row.get("backend_task_id") == target and row.get("objective")
            ),
            None,
        )
        state = self.states.get(target, "unknown")
        # Preserve a terminal race as a missed opportunity, never a query fallback.
        if self.variant.variant_type != "addtask" and state in {
            "completed",
            "cancelled",
            "failed",
        }:
            self.status = "target_finished_before_request"
            self.target_requested = True
            return
        self._queue("followup", self.variant.followup_request, timestamp)
        self.target_requested = True
        self.reply_text = ""
        self.generated_request = {
            "resolved_intent": {
                "operation": self.variant.variant_type,
                "state": state,
                "reference_mode": role,
                "fallback_used": False,
            },
            "generator": "authored",
            "text": self.variant.followup_request,
        }

    def delivery_started(self, request_id, timestamp):
        """Record the first actually played speech, independently of queueing."""
        if request_id == self._initial_id() and self.initial_started is None:
            self.initial_started = timestamp
        for row in self.requests:
            if row["request_id"] == request_id:
                if row["delivery"]["delivery_started_at"] is None:
                    row["delivery"]["delivery_started_at"] = timestamp

    def delivery_completed(self, request_id, timestamp):
        """Only complete delivery authorizes target selection and result scoring."""
        if request_id == self._initial_id():
            self.initial_delivery = timestamp
        for row in self.requests:
            if row["request_id"] == request_id:
                row["delivery"]["delivered_at"] = timestamp
                if row["phase"] == "followup":
                    self.reply_deadline = (
                        timestamp + self.config.gates.reply_timeout_seconds
                    )
                    self.status = "delivered"

    def on_delivery(self, role, text, event_id, timestamp):
        if role == "user" and event_id:
            self.delivery_started(event_id, timestamp)
        elif role == "agent" and self.status == "delivered" and text:
            if self.delivery["reply_started_at"] is None:
                self.delivery["reply_started_at"] = timestamp
            self.reply_text = f"{self.reply_text} {text}".strip()

    def finalize(self, termination_reason, timestamp):
        self._drain()
        summary = super().finalize(termination_reason, timestamp)
        summary["version"] = "interaction-variant-evidence-v1"
        summary["mechanism"] = "interaction_variant"
        if (
            self.backend_task_id is None
            and self.initial_delivery is not None
            and self.status != "unsupported"
        ):
            summary["status"] = self.status = "target_unbound"
        summary.update(
            variant_type=self.variant.variant_type,
            requests=self.requests,
            bindings=self.bindings,
            binding_status=self.binding_status,
            acceptances=self.acceptances,
            variant_id=self.variant.id,
        )
        summary["binding"]["role"] = (
            "temporary" if self.variant.variant_type == "cancel" else "primary"
        )
        return summary

    def _score(self, scoring=None):
        if self.variant.variant_type == "addtask" or self.status in {
            "target_unbound",
            "target_finished_before_request",
            "queued",
        }:
            return {
                "status": "not_applicable"
                if self.variant.variant_type == "addtask"
                else self.status,
                "completion_score": None,
                "evaluation_log": {
                    "native_result": {"passed": None},
                    "reply": {"passed": None, "status": "not_evaluated"},
                },
            }
        if (
            self.variant.variant_type == "cancel"
            and "temporary" in self.binding_status
            and self.binding_status.get("temporary") != "bound"
        ):
            return {
                "status": "target_unbound",
                "completion_score": None,
                "evaluation_log": {
                    "native_result": {
                        "passed": None,
                        "reason": self.binding_status.get("temporary", "unbound"),
                    },
                    "reply": {"passed": None, "status": "not_evaluated"},
                },
            }
        if (
            self.variant.variant_type == "modify"
            and "primary" in self.binding_status
            and self.binding_status.get("primary") != "bound"
        ):
            return {
                "status": "target_unbound",
                "completion_score": None,
                "evaluation_log": {
                    "native_result": {
                        "passed": None,
                        "reason": self.binding_status.get("primary", "unbound"),
                    },
                    "reply": {"passed": None, "status": "not_evaluated"},
                },
            }
        return super().score(scoring)

    def score(self, scoring=None):
        """Evaluate the completed mechanism evidence through its evaluator."""
        from tau2.metrics.active.persistent_task.variant_evaluator import (
            InteractionVariantEvaluator,
        )

        return InteractionVariantEvaluator.score(self, scoring)


# Compatibility for callers that imported the old runtime name.
InteractionVariantRuntime = InteractionVariantController

"""Live runtime for the dynamic persistent-task metric."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from tau2.metrics.active.config import PersistentTaskMetricConfig
from tau2.metrics.active.persistent_task.generator import (
    RequestGenerationContext,
    RequestGenerator,
)
from tau2.metrics.active.persistent_task.policy import PersistentTaskPolicy
from tau2.persistent_tasks.availability import evidence_source
from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.scores import consolidate_score


class PersistentTaskMetricRuntime:
    """Observe one primary backend task and inject at most one request."""

    name = "persistent_task"

    def __init__(
        self, config: PersistentTaskMetricConfig, *, orchestrator, seed, trial
    ):
        self.config = config
        self.orchestrator = orchestrator
        self.seed = self._seed(seed, orchestrator.task.id, trial)
        self.source = None
        self.capabilities: dict[str, Any] = {}
        self.policy = PersistentTaskPolicy(config, self.seed)
        root = Path(__file__).resolve().parents[5]
        self.generator = RequestGenerator(
            config.generator,
            root / "data" / "tau2" / "metrics" / "persistent_task" / "templates",
        )
        self.backend_task_id: str | None = None
        self.source_task_id: str | None = None
        self.source_stage: int | None = None
        self.task_label: str | None = None
        self.request_id: str | None = None
        self.request_text: str | None = None
        self.generated_request: dict[str, Any] | None = None
        self.status = "pending"
        self.issues: list[dict[str, Any]] = []
        self.operations: list[dict[str, Any]] = []
        self.delivery: dict[str, Any] = {
            "queued_at": None,
            "delivery_started_at": None,
            "delivered_at": None,
            "reply_started_at": None,
            "reply_completed_at": None,
        }
        self.reply_steps = 0
        self.reply_deadline: float | None = None
        self.reply_text = ""
        self.observations: list[dict[str, Any]] = []

    @staticmethod
    def _seed(seed, task_id, trial):
        value = f"{seed}:{task_id}:{trial}:persistent-task-metric-v1".encode()
        return int.from_bytes(hashlib.sha256(value).digest()[:8], "big")

    def install(self) -> None:
        self.source = evidence_source(self.orchestrator.agent)
        if self.source is None:
            self.status = "unsupported"
            self.issues.append({"reason": "missing_evidence_source"})
            return
        self.capabilities = dict(self.source.get_capabilities())
        if not self.capabilities.get("task_identity"):
            self.status = "unsupported"
            self.issues.append({"reason": "missing_task_identity_capability"})
            return
        states = set(self.capabilities.get("states", []))
        operations = set(self.capabilities.get("operations", []))
        if self.policy.choose_preferred(states, operations) is None:
            self.issues.append({"reason": "no_supported_preferred_option"})
            return
        self.source.enable()
        runtime = getattr(self.orchestrator.environment, "scenario_runtime", None)
        if runtime is not None:
            runtime.add_delivery_listener(self.on_delivery)

    def _timestamp(self, event: BackendTaskObservation) -> float:
        runtime = getattr(self.orchestrator.environment, "scenario_runtime", None)
        if runtime is not None and event.received_monotonic is not None:
            return runtime.timestamp_at(event.received_monotonic)
        return runtime.time() if runtime is not None else event.timestamp

    def _observe(self, event: BackendTaskObservation) -> None:
        if event.backend_task_id is None:
            return
        event = replace(event, timestamp=self._timestamp(event))
        if self.backend_task_id is None:
            if event.kind not in {
                "task_created",
                "task_state_changed",
                "task_result",
                "backend_task_state",
            }:
                return
            self.backend_task_id = event.backend_task_id
            self.source_task_id = event.source_task_id
            self.source_stage = event.source_stage
            self.task_label = event.objective
        if event.backend_task_id != self.backend_task_id:
            return
        self.observations.append(event.dump())
        if event.kind.startswith("native_operation_"):
            self.operations.append(event.dump())
        self.policy.observe(event)

    def _drain(self) -> None:
        if self.source is None or self.status in {
            "unsupported",
            "generation_unavailable",
            "completed",
            "reply_unavailable",
        }:
            return
        for event in self.source.drain_events():
            self._observe(event)

    def advance(self, timestamp: float, turn_boundary: bool) -> None:
        if self.status in {
            "unsupported",
            "generation_unavailable",
            "completed",
            "reply_unavailable",
        }:
            return
        self._drain()
        if self.status == "delivered" and self.reply_text and turn_boundary:
            self.delivery["reply_completed_at"] = timestamp
            self.status = "completed"
        if turn_boundary:
            self.policy.on_turn_boundary()
        intent = self.policy.resolve(timestamp)
        if intent is None or self.request_id is not None:
            if self.policy.decision_status in {"expired", "unresolved"}:
                self.status = self.policy.decision_status
            return
        self.status = "generating"
        language_context = getattr(
            getattr(self.orchestrator, "call_context", None), "language", None
        )
        language = (
            self.config.language
            or getattr(language_context, "provider_language", None)
            or getattr(language_context, "language", None)
            or "en"
        )
        try:
            generated = self.generator.generate(
                RequestGenerationContext(intent, language, self.task_label, self.seed)
            )
        except RuntimeError as error:
            self.status = "generation_unavailable"
            self.issues.append({"reason": str(error)})
            return
        self.request_id = (
            f"persistent_task_metric_{self.orchestrator.simulation_id}_request_1"
        )
        self.request_text = generated.text
        self.generated_request = {
            "text": generated.text,
            "generator": generated.generator,
            "language": generated.language,
            "state": generated.state,
            "operation": generated.operation,
            "fallback_reason": generated.fallback_reason,
            "resolved_intent": {
                "state": intent.state.value,
                "operation": intent.operation,
                "reference_mode": intent.reference_mode,
                "fallback_used": intent.fallback_used,
                "fallback_reason": intent.fallback_reason,
            },
        }
        runtime = getattr(self.orchestrator.environment, "scenario_runtime", None)
        if runtime is None:
            self.status = "unsupported"
            self.issues.append({"reason": "missing_scenario_runtime"})
            return
        queue = getattr(runtime, "queue_user_input", None)
        if queue is None:
            with runtime.lock:
                runtime.user_inputs.append((self.request_id, self.request_text))
        else:
            queue(
                self.request_id,
                self.request_text,
                source="persistent_task",
                purpose="followup",
            )
        self.delivery["queued_at"] = timestamp
        self.status = "queued"

    def on_delivery(self, role, text, event_id, timestamp) -> None:
        if self.request_id is None:
            return
        if role == "user" and event_id == self.request_id:
            self.delivery["delivery_started_at"] = timestamp
            self.delivery["delivered_at"] = timestamp
            self.delivery["reply_started_at"] = timestamp
            self.reply_deadline = timestamp + self.config.gates.reply_timeout_seconds
            self.status = "delivered"
            return
        if role == "agent" and self.status == "delivered" and text:
            self.reply_text = f"{self.reply_text} {text}".strip()

    def should_extend_reply(self, timestamp: float) -> bool:
        if self.status != "delivered":
            return False
        if self.reply_deadline is not None and timestamp >= self.reply_deadline:
            self.status = "reply_unavailable"
            return False
        return self.reply_steps < self.config.gates.reply_max_steps

    def consume_reply_step(self, timestamp: float) -> None:
        if self.status == "delivered":
            self.reply_steps += 1
            if not self.should_extend_reply(timestamp):
                self.status = "reply_unavailable"

    def finalize(self, termination_reason: str, timestamp: float) -> dict[str, Any]:
        if self.delivery["delivered_at"] is None and self.status in {
            "pending",
            "generating",
            "queued",
        }:
            self.status = "not_reached"
        elif self.status == "delivered":
            self.status = "reply_unavailable"
        return {
            "version": "persistent-task-metric-evidence-v1",
            "status": self.status,
            "policy_version": self.config.policy_version,
            "seed": self.seed,
            "binding": {
                "role": "primary",
                "backend_task_id": self.backend_task_id,
                "source_task_id": self.source_task_id,
                "source_stage": self.source_stage,
                "task_label": self.task_label,
            },
            "observations": self.observations,
            "preferred_intent": (
                {
                    "state": self.policy.preferred.state.value,
                    "operation": self.policy.preferred.operation,
                }
                if self.policy.preferred
                else None
            ),
            "resolved_intent": self.generated_request.get("resolved_intent")
            if self.generated_request
            else None,
            "generated_request": self.generated_request,
            "request_id": self.request_id,
            "delivery": self.delivery,
            "reply_text": self.reply_text,
            "operations": self.operations,
            "termination_reason": termination_reason,
            "finalized_at": timestamp,
            "issues": self.issues,
        }

    def close(self) -> None:
        return None

    def score(self, scoring=None) -> dict[str, Any]:
        """Score the resolved request independently of the task reward."""
        if self.status in {
            "unsupported",
            "generation_unavailable",
            "unresolved",
            "not_reached",
            "reply_unavailable",
        }:
            return consolidate_score(
                {
                    "status": self.status,
                    "completion_score": None,
                    "evaluation_log": {
                        "operation_reward": None,
                        "native_result": {"passed": None, "reason": self.status},
                        "reply": {
                            "passed": None,
                            "status": "not_evaluated",
                            "reason": self.status,
                        },
                    },
                }
            )
        resolved = (self.generated_request or {}).get("resolved_intent") or {}
        operation_name = resolved.get("operation")
        candidates = [
            event
            for event in self.operations
            if event.get("backend_task_id") == self.backend_task_id
            and event.get("kind") != "native_operation_started"
            and self.delivery["delivered_at"] is not None
            and event.get("timestamp", 0) >= self.delivery["delivered_at"]
            and event.get("success") is not False
            and event.get("result") is not None
            and next(
                (
                    start.get("timestamp", 0)
                    for start in self.operations
                    if event.get("call_id")
                    and start.get("call_id") == event["call_id"]
                    and start.get("kind") == "native_operation_started"
                ),
                event.get("timestamp", 0),
            )
            >= self.delivery["delivered_at"]
        ]
        operation = next(
            (
                event
                for event in candidates
                if event.get("operation") == operation_name
                or (operation_name == "result" and event.get("operation") == "query")
            ),
            None,
        )
        operation_reward = float(operation is not None)
        result = operation.get("result") if operation else None
        actual_state = (
            (result.get("state") if isinstance(result, dict) else None)
            or (operation.get("state") if operation else None)
            or (operation.get("status") if operation else None)
        )
        native_reward = float(
            operation_reward
            and (
                (operation_name == "query" and actual_state is not None)
                or (
                    operation_name == "result"
                    and actual_state == "completed"
                    and isinstance(result, dict)
                    and result.get("result_available", True)
                )
                or (
                    operation_name == "modify"
                    and isinstance(result, dict)
                    and result.get("accepted") is True
                )
                or (
                    operation_name == "cancel"
                    and isinstance(result, dict)
                    and result.get("effect") == "cancelled"
                )
            )
        )
        reply_reward = 0.0
        reply_detail = {
            "passed": None,
            "status": "not_evaluated",
            "reason": "native_result_not_passed",
        }
        if native_reward and not self.reply_text:
            reply_detail = {
                "passed": False,
                "status": "no_reply",
                "reason": "no_delivered_reply",
            }
        if self.reply_text and native_reward:
            fact = json.dumps(
                {
                    "operation": operation_name,
                    "actual_state": actual_state,
                    "native_operation_result": result,
                    "backend_task_id": self.backend_task_id,
                    "task_objective": self.task_label,
                    "requirement": "Accurately report the actual native task result without inventing completion or errors.",
                },
                ensure_ascii=False,
            )
            if scoring is None:
                reply_reward = 1.0
                reply_detail = {
                    "passed": True,
                    "status": "presence_only",
                    "reason": "scoring_not_configured",
                }
            else:
                from tau2.data_model.message import AssistantMessage
                from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

                verdict = CommunicateEvaluator.evaluate_communicate_info(
                    [AssistantMessage(role="assistant", content=self.reply_text)],
                    [fact],
                    scoring=scoring,
                )[0]
                reply_reward = float(verdict.met)
                reply_detail = {
                    "passed": verdict.met,
                    "status": "judged",
                    "reason": verdict.justification,
                }
        reward = native_reward * reply_reward
        return {
            "status": "measured",
            "completion_score": reward,
            "evaluation_log": {
                "operation_reward": operation_reward,
                "native_result": {
                    "passed": bool(native_reward),
                    "operation": operation,
                    "actual_state": actual_state,
                },
                "reply": {**reply_detail, "text": self.reply_text},
            },
            "operation": operation,
            "actual_state": actual_state,
        }

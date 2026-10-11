"""V6 observes the frozen agent's recovery without repairing its execution."""

from __future__ import annotations

import json
try:
    from typing import override
except ImportError:
    from typing_extensions import override

from audio_driver import InfrastructureError, expected_interruption, service_error_cause
from native_recovery import (
    VERSION,
    WATCHDOG_MESSAGE,
    NativeRecovery,
    external_service_cause,
)
from protocol import TERMINAL
from task_driver import TaskDriver, TaskProtocol


class PassiveProtocol(TaskProtocol):
    version = VERSION

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_recovery = NativeRecovery()

    @override
    def metrics(self):
        result = super().metrics()
        result["native_recovery"] = self.native_recovery.summary(self.elapsed() or 0)
        return result


class PassiveDriver(TaskDriver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not isinstance(self.protocol, PassiveProtocol):
            raise TypeError("V6 requires PassiveProtocol")
        self.native_recovery = self.protocol.native_recovery

    def recovery_scope_ready(self):
        return (
            self.input_done
            and any(text.strip() for text in self.user_context)
            and not self.pending_input_items
            and not any(
                not p.cancelled and not p.recorded and (p.started or p.pcm)
                for p in self.responses.values()
            )
            and not any(
                not d.committed and not d.cancelled
                for d in self.protocol.deliveries.values()
            )
            and not any(not f.done() for f in self.delivery_waiters.values())
        )

    def terminal_inactivity_ready(self):
        if (
            not self.recovery_scope_ready()
            or self.runtime.waiting
            or self.runtime.operations.locked()
            or self.backend_active
        ):
            return False
        if (
            self.contract.get("kind") == "action_only"
            and not self.contract.get("requires_answer", False)
        ):
            return True
        return self.protocol.state in TERMINAL and any(
            delivery.committed
            and not delivery.cancelled
            and delivery.kind in {"final", "clarification", "failure"}
            for delivery in self.protocol.deliveries.values()
        )

    @override
    async def observe_gateway_event(self, event):
        import time

        kind = event.get("type")
        payload = (
            event.get("payload", {}) if kind == "evaluation.provider_event" else event
        )
        cause = external_service_cause(payload)
        if kind in {"error", "voice.connection", "evaluation.provider_event"}:
            message = (
                str(
                    payload.get("message")
                    or (payload.get("error") or {}).get("message", "")
                )
                if isinstance(payload.get("error", {}), dict)
                else str(payload.get("message", ""))
            )
            text_cause = service_error_cause({"message": message})
            if not cause and text_cause in {"provider_billing", "provider_unavailable"}:
                cause = text_cause
            if cause:
                raise InfrastructureError(
                    "Explicit external service error: " + message, cause
                )
        if kind == "error":
            message = str(event.get("message", ""))
            if "Cannot cancel response before response.created" in message:
                self.protocol.record(
                    "agent.runtime_event",
                    kind="benign_cancel_race",
                    origin="qwen_gateway",
                    message=message,
                )
                return True
            if expected_interruption(
                event, self.last_user_interruption, time.monotonic()
            ):
                return True
            if event.get("message") != WATCHDOG_MESSAGE:
                raise InfrastructureError(
                    event.get("message", "Gateway error"), "unclassified_gateway_error"
                )
            self.protocol.record(
                "agent.runtime_event",
                kind="response_start_watchdog",
                origin="qwen_gateway",
                cause="not_established",
                message=event["message"],
            )
            if not self.native_recovery.pending and not self.recovery_scope_ready():
                raise InfrastructureError(
                    "Native watchdog outside confirmed-input/no-pending-speech scope",
                    "unsupported_recovery_state",
                )
            if self.native_recovery.start(self.protocol.elapsed() or 0):
                self.window.reset()
                self.protocol.record(
                    "native.recovery.started",
                    episode_id=self.native_recovery.episodes[-1]["id"],
                    started_sec=self.native_recovery.episodes[-1]["started_sec"],
                )
            return True
        if kind == "voice.ready" and event.get("inputSampleRate", 16000) != 16000:
            raise InfrastructureError("Gateway input rate differs from packaged audio")
        if kind in {
            "voice.connection",
            "voice.ready",
            "evaluation.connection.recovering",
        }:
            if self.done:
                return True
            if self.native_recovery.pending:
                observed = self.protocol.elapsed() or 0
                self.native_recovery.connection(event, observed)
                self.protocol.record(
                    "native.recovery.progress",
                    connection_event=event,
                    observed_sec=observed,
                )
                return True
            if (
                kind == "evaluation.connection.recovering"
                and event.get("reason") == "inactivity"
                and self.terminal_inactivity_ready()
            ):
                self.protocol.record(
                    "connection.inactivity_after_terminal_work",
                    origin="qwen_gateway",
                    reason=event.get("reason"),
                    request_state=self.protocol.state,
                )
                await self.freeze("idle_window")
                self.done = True
                return True
            if kind == "evaluation.connection.recovering" or (
                kind == "voice.connection" and event.get("state") == "unavailable"
            ):
                raise InfrastructureError(
                    "Recovery without an admitted native watchdog",
                    "unsupported_recovery_state",
                )
        return False

    @override
    def begin_recovery(self):
        raise InfrastructureError(
            "Active adapter recovery is disabled in v6", "unsupported_recovery_state"
        )

    @override
    async def restore_deliveries(self):
        raise InfrastructureError(
            "Adapter redelivery is disabled in v6", "unsupported_recovery_state"
        )

    @override
    def locally_idle(self, state, proxy):
        return not self.native_recovery.pending and super().locally_idle(state, proxy)

    @override
    async def advance_observation(self, state):
        episode = None
        candidate = self.native_recovery.active
        probe_started = None
        if candidate is not None and candidate["ready_event_sec"] is not None:
            ready_event = candidate["ready_event_sec"]
            probe_started = self.protocol.elapsed() or 0
            fresh = await self.control("state")
            if (
                self.native_recovery.active is candidate
                and candidate["ready_event_sec"] == ready_event
            ):
                state = fresh
                episode = self.native_recovery.confirm(
                    state, self.protocol.elapsed() or 0
                )
        if episode:
            self.recoveries += 1
            self.recovery_wait_sec += episode["ended_sec"] - episode["started_sec"]
            self.window.reset()
            self.protocol.record(
                "native.recovery.ready_confirmed",
                ready_snapshot=state.get("ready"),
                readiness_probe_started_sec=probe_started,
                **episode,
            )
        return await super().advance_observation(state)

    def close_recovery_evidence(self, reason):
        active = self.native_recovery.active
        self.native_recovery.finish(reason, self.protocol.elapsed() or 0)
        if active is not None:
            self.protocol.record("native.recovery.ended", **active)
        (self.output / "native-recovery.json").write_text(
            json.dumps(
                self.native_recovery.summary(self.protocol.elapsed() or 0), indent=2
            )
            + "\n"
        )

    @override
    def finalize_projection(self):
        self.close_recovery_evidence(self.observation.end_reason)
        super().finalize_projection()

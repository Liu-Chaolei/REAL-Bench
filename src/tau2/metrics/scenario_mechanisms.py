"""Runtime manager for mechanisms that change a scenario's interaction flow."""

from __future__ import annotations

from typing import Any

from tau2.metrics.active.config import PersistentTaskMetricConfig
from tau2.metrics.active.persistent_task.variant import InteractionVariantController


class ScenarioMechanismManager:
    """Install and drive authored scenario mechanisms independently of metrics."""

    def __init__(self, *, orchestrator, variant, config=None, seed=0, trial=None):
        self.orchestrator = orchestrator
        self.variant = variant
        self.config = config or PersistentTaskMetricConfig(
            preferred_options=[dict(state="running", operation="query", weight=1)]
        )
        self.seed = seed or 0
        self.trial = trial or 0
        self.mechanisms = [
            InteractionVariantController(
                self.config,
                variant=variant,
                orchestrator=orchestrator,
                seed=self.seed,
                trial=self.trial,
            )
        ]
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        for mechanism in self.mechanisms:
            mechanism.install()
        runtime = getattr(self.orchestrator.environment, "scenario_runtime", None)
        if runtime is not None:
            runtime.add_delivery_listener(self.on_delivery)
        self._installed = True

    def advance(self, timestamp: float, turn_boundary: bool) -> None:
        for mechanism in self.mechanisms:
            mechanism.advance(timestamp, turn_boundary)

    def on_delivery(self, role, text, event_id, timestamp) -> None:
        for mechanism in self.mechanisms:
            mechanism.on_delivery(role, text, event_id, timestamp)

    def should_extend_reply(self, timestamp: float) -> bool:
        return any(
            mechanism.should_extend_reply(timestamp) for mechanism in self.mechanisms
        )

    def delivery_started(self, request_id, timestamp):
        for mechanism in self.mechanisms:
            mechanism.delivery_started(request_id, timestamp)

    def delivery_completed(self, request_id, timestamp):
        for mechanism in self.mechanisms:
            mechanism.delivery_completed(request_id, timestamp)

    def consume_reply_step(self, timestamp: float) -> None:
        for mechanism in self.mechanisms:
            mechanism.consume_reply_step(timestamp)

    def finalize(self, termination_reason: str, timestamp: float) -> dict[str, Any]:
        self._summary = {
            "version": "scenario-mechanisms-v1",
            "mechanisms": {
                mechanism.name: mechanism.finalize(termination_reason, timestamp)
                for mechanism in self.mechanisms
            },
        }
        return self._summary

    def score(self, scoring=None) -> dict[str, Any]:
        from tau2.metrics.active.persistent_task.variant_evaluator import (
            score_interaction_variant,
        )

        return {
            name: score_interaction_variant(summary, scoring)
            for name, summary in self._summary["mechanisms"].items()
        }

    def close(self) -> None:
        for mechanism in self.mechanisms:
            mechanism.close()

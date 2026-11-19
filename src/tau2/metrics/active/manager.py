"""Coordinator for active metrics during a live simulation."""

from __future__ import annotations

from typing import Any

from tau2.metrics.active.config import ActiveMetricsConfig
from tau2.metrics.active.persistent_task.runtime import PersistentTaskMetricRuntime


class ActiveMetricManager:
    """Install and drive configured active metrics in orchestration order."""

    def __init__(self, config: ActiveMetricsConfig, *, orchestrator, seed, trial=None):
        self.config = config
        self.orchestrator = orchestrator
        self.seed = seed or 0
        self.trial = trial or 0
        self.metrics = []
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        if self.config.persistent_task and self.config.persistent_task.enabled:
            self.metrics.append(
                PersistentTaskMetricRuntime(
                    self.config.persistent_task,
                    orchestrator=self.orchestrator,
                    seed=self.seed,
                    trial=self.trial,
                )
            )
        for metric in self.metrics:
            metric.install()
        self._installed = True

    def advance(self, timestamp: float, turn_boundary: bool) -> None:
        for metric in self.metrics:
            metric.advance(timestamp, turn_boundary)

    def on_delivery(self, role, text, event_id, timestamp) -> None:
        for metric in self.metrics:
            metric.on_delivery(role, text, event_id, timestamp)

    def should_extend_reply(self, timestamp: float) -> bool:
        return any(metric.should_extend_reply(timestamp) for metric in self.metrics)

    def consume_reply_step(self, timestamp: float) -> None:
        for metric in self.metrics:
            metric.consume_reply_step(timestamp)

    def finalize(self, termination_reason: str, timestamp: float) -> dict[str, Any]:
        summary = {
            "version": "active-metrics-v1",
            "config_digest": self.config.fingerprint(),
            "metrics": {
                metric.name: metric.finalize(termination_reason, timestamp)
                for metric in self.metrics
            },
        }
        self._summary = summary
        return summary

    def score(self, scoring=None) -> dict[str, Any]:
        return {
            metric.name: metric.score(scoring)
            for metric in self.metrics
        }

    def close(self) -> None:
        for metric in self.metrics:
            metric.close()


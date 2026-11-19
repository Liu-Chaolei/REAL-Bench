"""Deterministic trigger selection and fallback resolution."""

from __future__ import annotations

import random
from dataclasses import dataclass

from tau2.metrics.active.config import (
    FallbackRule,
    PersistentTaskMetricConfig,
    PreferredOption,
)
from tau2.persistent_tasks.core.models import (
    BackendTaskObservation,
    CanonicalState,
)


@dataclass(frozen=True)
class ResolvedIntent:
    state: CanonicalState
    operation: str
    reference_mode: str
    fallback_used: bool = False
    fallback_reason: str | None = None


class PersistentTaskPolicy:
    """Choose one preferred intent and resolve it against live observations."""

    def __init__(self, config: PersistentTaskMetricConfig, seed: int):
        self.config = config
        self.seed = seed
        self.rng = random.Random(seed)
        self.preferred: PreferredOption | None = None
        self.resolved: ResolvedIntent | None = None
        self.observations: list[BackendTaskObservation] = []
        self.current_state: CanonicalState | None = None
        self.entered_at: float | None = None
        self.created_at: float | None = None
        self.turn_boundaries = 0
        self.result_available = False
        self.decision_status = "pending"
        self.fallback_reason: str | None = None

    def choose_preferred(self, supported_states=None, supported_operations=None):
        states = supported_states or {option.state for option in self.config.preferred_options}
        operations = supported_operations or {
            option.operation for option in self.config.preferred_options
        }
        options = [
            option
            for option in self.config.preferred_options
            if option.state in states and option.operation in operations
        ]
        if not options:
            self.decision_status = "unsupported"
            return None
        self.preferred = self.rng.choices(
            options, weights=[option.weight for option in options], k=1
        )[0]
        return self.preferred

    def observe(self, observation: BackendTaskObservation) -> None:
        if observation.status is None:
            return
        try:
            state = CanonicalState(observation.status)
        except ValueError:
            return
        self.observations.append(observation)
        if self.current_state != state:
            self.current_state = state
            self.entered_at = observation.timestamp
        self.result_available = self.result_available or observation.result_available
        if self.created_at is None:
            self.created_at = observation.timestamp

    def on_turn_boundary(self) -> None:
        self.turn_boundaries += 1

    def _blocked_rule(self, state: CanonicalState) -> FallbackRule | None:
        if self.preferred is None:
            return None
        return next(
            (
                rule
                for rule in self.config.fallback_rules
                if rule.preferred_state == self.preferred.state
                and rule.blocked_state == state
            ),
            None,
        )

    def resolve(self, now: float) -> ResolvedIntent | None:
        if self.resolved is not None or self.preferred is None:
            return self.resolved
        if self.current_state is None:
            return None
        if self.created_at is not None and now - self.created_at > self.config.gates.max_wait_seconds:
            self.decision_status = "expired"
            return None
        if self.current_state == self.preferred.state:
            if self.config.gates.initial_state_guard and len(self.observations) <= 1:
                return None
            if len(self.observations) - 1 < self.config.gates.min_observations_after_creation:
                return None
            if self.entered_at is None or now - self.entered_at < self.config.gates.min_dwell_seconds:
                return None
            if self.turn_boundaries < self.config.gates.min_turn_boundaries:
                return None
            if (
                self.preferred.operation == "result"
                and not self.result_available
            ):
                return None
            self.decision_status = "preferred_ready"
            self.resolved = ResolvedIntent(
                self.preferred.state,
                self.preferred.operation,
                self.preferred.reference_mode,
            )
            return self.resolved
        rule = self._blocked_rule(self.current_state)
        if rule is None:
            if self.current_state in {
                CanonicalState.COMPLETED,
                CanonicalState.FAILED,
                CanonicalState.CANCELLED,
            }:
                self.decision_status = "unresolved"
            return None
        if rule.fallback_operation == "result" and not self.result_available:
            return None
        if self.entered_at is None or now - self.entered_at < self.config.gates.min_dwell_seconds:
            return None
        if self.turn_boundaries < self.config.gates.min_turn_boundaries:
            return None
        self.decision_status = "fallback_ready"
        self.fallback_reason = rule.reason
        self.resolved = ResolvedIntent(
            rule.fallback_state,
            rule.fallback_operation,
            self.preferred.reference_mode,
            fallback_used=True,
            fallback_reason=rule.reason,
        )
        return self.resolved


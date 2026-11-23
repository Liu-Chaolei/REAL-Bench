"""Configuration for active, trial-local metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.diagnostics.config import ModelProfile
from tau2.diagnostics.intent.io import digest
from tau2.persistent_tasks.core.models import CanonicalState

Operation = Literal["query", "result", "modify", "cancel"]


class PreferredOption(BaseModel):
    """One independently sampled trigger intent."""

    model_config = ConfigDict(extra="forbid")
    state: CanonicalState
    operation: Operation = "query"
    weight: float = Field(gt=0)
    reference_mode: str = "recent_task"

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "result" and self.state != CanonicalState.COMPLETED:
            raise ValueError("result intents require completed state")
        if self.state in {
            CanonicalState.NOT_STARTED,
            CanonicalState.UNKNOWN,
        }:
            raise ValueError("preferred intents require an observable state")
        return self


class FallbackRule(BaseModel):
    """Deterministic transition from an unreachable preferred state."""

    model_config = ConfigDict(extra="forbid")
    preferred_state: CanonicalState
    blocked_state: CanonicalState
    fallback_state: CanonicalState
    fallback_operation: Operation = "query"
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule(self):
        if self.preferred_state == self.blocked_state:
            raise ValueError("fallback rule cannot block its own preferred state")
        if (
            self.fallback_operation == "result"
            and self.fallback_state != CanonicalState.COMPLETED
        ):
            raise ValueError("result fallbacks require completed state")
        if self.fallback_state in {
            CanonicalState.NOT_STARTED,
            CanonicalState.UNKNOWN,
        }:
            raise ValueError("fallbacks require an observable state")
        return self


class MetricGates(BaseModel):
    """Guards that prevent creation-time or unstable-state triggers."""

    model_config = ConfigDict(extra="forbid")
    min_observations_after_creation: int = Field(default=2, ge=0)
    min_dwell_seconds: float = Field(default=3, ge=0)
    min_turn_boundaries: int = Field(default=1, ge=0)
    initial_state_guard: bool = True
    max_wait_seconds: float = Field(default=300, gt=0)
    reply_timeout_seconds: float = Field(default=30, gt=0)
    reply_max_steps: int = Field(default=8, ge=0)


class RequestGeneratorConfig(BaseModel):
    """Bounded LLM and template generation settings."""

    model_config = ConfigDict(extra="forbid")
    model: ModelProfile = Field(default_factory=ModelProfile)
    model_env: str | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=8, gt=0)
    max_attempts: int = Field(default=1, ge=0)
    max_characters: int = Field(default=300, ge=1)
    fallback_policy: Literal["call_language_required"] = "call_language_required"

    @model_validator(mode="after")
    def consistent_api_source(self):
        if self.model.api_role and any(
            (self.model_env, self.api_base_env, self.api_key_env)
        ):
            raise ValueError(
                "Shared active generator role conflicts with legacy connection overrides"
            )
        return self


class PersistentTaskMetricConfig(BaseModel):
    """Complete policy for one active persistent-task metric."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    policy_version: str = "persistent-task-metric-v1"
    preferred_options: list[PreferredOption] = Field(min_length=1)
    fallback_rules: list[FallbackRule] = Field(default_factory=list)
    gates: MetricGates = Field(default_factory=MetricGates)
    generator: RequestGeneratorConfig = Field(default_factory=RequestGeneratorConfig)
    language: str | None = None

    @model_validator(mode="after")
    def validate_rules(self):
        if not self.fallback_rules:
            generated: list[FallbackRule] = []
            for option in self.preferred_options:
                for blocked, operation in (
                    (CanonicalState.FAILED, "query"),
                    (CanonicalState.COMPLETED, "result"),
                    (CanonicalState.CANCELLED, "query"),
                ):
                    if blocked == option.state:
                        continue
                    if (
                        operation == "result"
                        and option.state == CanonicalState.COMPLETED
                    ):
                        continue
                    generated.append(
                        FallbackRule(
                            preferred_state=option.state,
                            blocked_state=blocked,
                            fallback_state=blocked,
                            fallback_operation=operation,
                            reason=f"{option.state.value}_blocked_by_{blocked.value}",
                        )
                    )
            self.fallback_rules = generated
        keys = [(r.preferred_state, r.blocked_state) for r in self.fallback_rules]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate fallback rule")
        preferred_states = {option.state for option in self.preferred_options}
        for rule in self.fallback_rules:
            if rule.preferred_state not in preferred_states:
                raise ValueError("fallback rule refers to an unknown preferred state")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "PersistentTaskMetricConfig":
        value = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(value.get("persistent_task", value))

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class ActiveMetricsConfig(BaseModel):
    """Run-level configuration for active metrics."""

    model_config = ConfigDict(extra="forbid")
    persistent_task: PersistentTaskMetricConfig | None = None

    @classmethod
    def load(cls, path: str | Path, api_config_dir=None) -> "ActiveMetricsConfig":
        value = yaml.safe_load(Path(path).read_text()) or {}
        value = value.get("active_metrics", value)
        if api_config_dir is not None and value.get("persistent_task"):
            generator = value["persistent_task"].setdefault("generator", {})
            if any(
                generator.get(k) for k in ("model_env", "api_base_env", "api_key_env")
            ):
                raise ValueError(
                    "api_config_dir conflicts with legacy active generator connection"
                )
            supplied = generator.get("model")
            if supplied and not supplied.get("api_role"):
                raise ValueError(
                    "api_config_dir conflicts with literal active generator model"
                )
            generator["model"] = {
                "api_role": "ACTIVE_REQUEST",
                "api_config_dir": str(api_config_dir),
            }
        return cls.model_validate(value)

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

    def enabled(self) -> bool:
        return bool(self.persistent_task and self.persistent_task.enabled)

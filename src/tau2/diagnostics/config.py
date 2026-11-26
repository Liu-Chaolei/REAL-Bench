"""Explicit model profiles and bounded independent diagnostic execution."""

import hashlib
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.config import (
    DEFAULT_DIAGNOSTIC_CONCURRENCY,
    DEFAULT_DIAGNOSTIC_MAX_CHARACTERS,
    DEFAULT_DIAGNOSTIC_MAX_REQUESTS,
    DEFAULT_DIAGNOSTIC_TIMEOUT_SECONDS,
    DEFAULT_SEMANTIC_COMMUNICATE_MODEL,
    DEFAULT_SEMANTIC_COMMUNICATE_MODEL_ARGS,
)
from tau2.diagnostics.conciseness.models import FeedbackConfig
from tau2.diagnostics.intent.io import digest


class ModelProfile(BaseModel):
    """Only environment references, never credentials, enter serialized config."""

    model_config = ConfigDict(extra="forbid")
    model: str = DEFAULT_SEMANTIC_COMMUNICATE_MODEL
    api_base: str | None = None
    api_key_env: str | None = None
    args: dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_SEMANTIC_COMMUNICATE_MODEL_ARGS)
    )

    api_identity: dict | None = None
    api_role: str | None = None
    api_config_dir: str | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_api_role(cls, value):
        if (
            isinstance(value, dict)
            and value.get("api_role")
            and not value.get("api_identity")
        ):
            from tau2.config import DEFAULT_API_CONFIG_DIR
            from tau2.utils.api_config import get_api_configuration

            value = dict(value)
            profile = get_api_configuration(
                value.get("api_config_dir") or DEFAULT_API_CONFIG_DIR
            ).text(value["api_role"])
            value.update(
                api_identity=profile.public(),
                model=profile.litellm_model,
                api_base=profile.public()["api_base"],
                api_key_env=None,
                args=dict(profile.args),
            )
        return value

    def arguments(self):
        if self.api_role:
            from tau2.config import DEFAULT_API_CONFIG_DIR
            from tau2.utils.api_config import get_api_configuration

            return (
                get_api_configuration(self.api_config_dir or DEFAULT_API_CONFIG_DIR)
                .text(self.api_role)
                .arguments()
            )
        result = dict(self.args)
        if self.api_base:
            result["api_base"] = self.api_base
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ValueError(f"Missing diagnostic credential: {self.api_key_env}")
            result["api_key"] = key
        return result

    @model_validator(mode="after")
    def no_literal_credentials(self):
        if any(k in self.args for k in ("api_key", "authorization", "headers")):
            raise ValueError("Use api_key_env for diagnostic credentials")
        return self


class MetricsConfig(BaseModel):
    """One frozen diagnostic profile, separate from the evaluated model."""

    model_config = ConfigDict(extra="forbid")
    measures: list[
        Literal[
            "intent",
            "execution",
            "voice",
            "interaction",
            "state_interaction",
            "conciseness",
            "persistent_task",
        ]
    ] = Field(default_factory=lambda: ["intent", "execution", "voice"])
    auto_score: bool = True
    concurrency: int = Field(default=DEFAULT_DIAGNOSTIC_CONCURRENCY, ge=1)
    max_requests: int = Field(default=DEFAULT_DIAGNOSTIC_MAX_REQUESTS, ge=1)
    timeout_seconds: float = Field(default=DEFAULT_DIAGNOSTIC_TIMEOUT_SECONDS, gt=0)
    max_characters: int = Field(default=DEFAULT_DIAGNOSTIC_MAX_CHARACTERS, ge=1)
    state_sidecar: Literal["off", "online_full"] = "off"
    state_sidecar_queue_size: int = Field(default=256, ge=1)
    state_sidecar_drain_timeout_seconds: float = Field(default=900.0, gt=0)
    model: ModelProfile = Field(default_factory=ModelProfile)
    models: dict[
        Literal[
            "gt",
            "prediction",
            "evaluation",
            "execution",
            "interaction",
            "state_interaction",
            "feedback_nuggets",
            "feedback_alignment",
        ],
        ModelProfile,
    ] = Field(default_factory=dict)
    conciseness: FeedbackConfig = Field(default_factory=FeedbackConfig)

    api_config_dir: str | None = None
    api_judge_role: Literal["JUDGE", "USER"] = "JUDGE"

    @model_validator(mode="before")
    @classmethod
    def resolve_api_profiles(cls, value):
        if isinstance(value, dict) and value.get("api_config_dir"):
            from tau2.utils.api_config import DIAGNOSTIC_ROLES

            value = dict(value)
            directory = value["api_config_dir"]
            if isinstance(value.get("model"), dict) and value["model"].get(
                "api_identity"
            ):
                return value
            supplied = [value.get("model", {}), *value.get("models", {}).values()]
            if any(
                isinstance(p, dict) and p and not p.get("api_role") for p in supplied
            ):
                raise ValueError(
                    "api_config_dir conflicts with literal diagnostic profiles"
                )
            default_role = value.get("api_judge_role", "JUDGE")
            value["model"] = {"api_role": default_role, "api_config_dir": directory}
            value["models"] = {
                role: {
                    "api_role": target if default_role == "JUDGE" else "USER",
                    "api_config_dir": directory,
                }
                for role, target in DIAGNOSTIC_ROLES.items()
            }
        return value

    @classmethod
    def load(cls, path, api_config_dir=None, judge_from_user=False):
        path = Path(path)
        value = yaml.safe_load(path.read_text())
        if api_config_dir is not None:
            value["api_config_dir"] = str(api_config_dir)
        if judge_from_user:
            value["api_judge_role"] = "USER"
        if value.get("api_config_dir"):
            for profile in [value.get("model", {}), *value.get("models", {}).values()]:
                if profile.get("api_role"):
                    profile.pop("api_identity", None)
        feedback = value.get("conciseness", {})
        if isinstance(feedback.get("calibration"), str):
            import json

            calibration_path = path.parent / feedback["calibration"]
            data = json.loads(calibration_path.read_text())
            calibration = dict(data.get("calibration", data))
            calibration.setdefault(
                "source_sha256",
                hashlib.sha256(calibration_path.read_bytes()).hexdigest(),
            )
            feedback["calibration"] = calibration
        return cls.model_validate(value)

    def profile(self, role):
        return self.models.get(role, self.model)

    def fingerprint(self):
        return digest(self.model_dump(mode="json"))

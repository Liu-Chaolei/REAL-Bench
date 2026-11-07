"""Explicit model profiles and bounded independent diagnostic execution."""

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

    def arguments(self):
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
    measures: list[Literal["intent", "execution", "voice"]] = Field(
        default_factory=lambda: ["intent", "execution", "voice"]
    )
    auto_score: bool = True
    concurrency: int = Field(default=DEFAULT_DIAGNOSTIC_CONCURRENCY, ge=1)
    max_requests: int = Field(default=DEFAULT_DIAGNOSTIC_MAX_REQUESTS, ge=1)
    timeout_seconds: float = Field(default=DEFAULT_DIAGNOSTIC_TIMEOUT_SECONDS, gt=0)
    max_characters: int = Field(default=DEFAULT_DIAGNOSTIC_MAX_CHARACTERS, ge=1)
    model: ModelProfile = Field(default_factory=ModelProfile)
    models: dict[
        Literal["gt", "prediction", "evaluation", "execution"], ModelProfile
    ] = Field(default_factory=dict)

    @classmethod
    def load(cls, path):
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def profile(self, role):
        return self.models.get(role, self.model)

    def fingerprint(self):
        return digest(self.model_dump(mode="json"))

"""Configuration and structured output for contextual scenario requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DynamicFollowupConfig(BaseModel):
    """Opt-in runtime rendering of later fixed scenario goals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    stages: list[int] = Field(default_factory=lambda: [1, 2])
    max_context_characters: int = Field(default=24000, ge=2000, le=100000)
    max_retries: int = Field(default=1, ge=0, le=3)

    @field_validator("stages")
    @classmethod
    def valid_stages(cls, value):
        if len(value) != len(set(value)) or any(stage not in {1, 2} for stage in value):
            raise ValueError(
                "dynamic follow-up stages must be unique values from [1, 2]"
            )
        return value


class DynamicFollowupContext(BaseModel):
    """LLM classification of what the caller believes about prior work."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "completed",
        "partial",
        "incomplete",
        "failed",
        "timed_out",
        "abandoned",
        "unknown",
    ]
    affect: str = Field(min_length=1, max_length=80)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


class DynamicFollowupCandidate(BaseModel):
    """A contextual utterance whose business goal remains the source request."""

    model_config = ConfigDict(extra="forbid")

    utterance: str = Field(min_length=1)
    covered_requirements: list[str] = Field(default_factory=list)
    referenced_entities: list[str] = Field(default_factory=list)
    new_requirements: list[str] = Field(default_factory=list)


class DynamicFollowupVerification(BaseModel):
    """Independent semantic check of a generated later-stage request."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    reason: str = ""
    missing_requirements: list[str] = Field(default_factory=list)
    invented_requirements: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    extra_tools: list[str] = Field(default_factory=list)
    leaked_information: list[str] = Field(default_factory=list)
    unsupported_historical_claims: list[str] = Field(default_factory=list)

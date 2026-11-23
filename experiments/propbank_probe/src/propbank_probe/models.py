"""Serializable models used by the isolated PropBank probe."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArgumentRecord(BaseModel):
    """One predicate argument or adjunct span."""

    model_config = ConfigDict(extra="forbid")

    role: str
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_start: int | None = Field(default=None, ge=0)
    token_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_span(self):
        if self.char_end < self.char_start:
            raise ValueError("character span is reversed")
        if self.token_start is not None and self.token_end is not None:
            if self.token_end < self.token_start:
                raise ValueError("token span is reversed")
        return self


class PredicateRecord(BaseModel):
    """One normalized PropBank predicate frame."""

    model_config = ConfigDict(extra="forbid")

    predicate_text: str
    lemma: str | None = None
    sense: str | None = None
    sense_status: Literal["emitted", "not_emitted", "ambiguous", "unresolved"]
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_start: int | None = Field(default=None, ge=0)
    token_end: int | None = Field(default=None, ge=0)
    arguments: list[ArgumentRecord] = Field(default_factory=list)
    adjuncts: list[ArgumentRecord] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SentenceParse(BaseModel):
    """Normalized output for one input sentence."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    text: str
    backend: str
    model: str
    status: Literal["complete", "error", "unsupported"]
    predicates: list[PredicateRecord] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class CaseRecord(BaseModel):
    """One probe input."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    category: str
    source: str = "hand_authored"


class MappingParameter(BaseModel):
    """A candidate Gaia2 parameter retaining its PropBank source."""

    name: str
    text: str
    source_role: str
    source_span: tuple[int, int]


class PredicateMapping(BaseModel):
    """Candidate Gaia2 action mapping for one predicate."""

    case_id: str
    predicate: str
    lemma: str | None = None
    sense: str | None = None
    candidate_actions: list[str] = Field(default_factory=list)
    parameters: list[MappingParameter] = Field(default_factory=list)
    status: Literal["mapped", "ambiguous", "unmapped", "unsupported"]
    reason: str | None = None


class CompositionResult(BaseModel):
    """Deterministic connective analysis for one sentence."""

    case_id: str
    status: Literal["candidate", "not_applicable", "unsupported"]
    relations: list[dict[str, Any]] = Field(default_factory=list)
    reason: str | None = None


class ExpectedCase(BaseModel):
    """Small, manually authored gold record for the probe report."""

    model_config = ConfigDict(extra="allow")

    id: str
    propbank: dict[str, Any] = Field(default_factory=dict)
    gaia_mapping: dict[str, Any] = Field(default_factory=dict)
    composition: dict[str, Any] = Field(default_factory=dict)


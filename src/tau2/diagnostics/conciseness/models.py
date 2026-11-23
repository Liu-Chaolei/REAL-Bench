"""Strict contracts for frozen feedback obligations and independent alignment."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.config import DEFAULT_FEEDBACK_EVIDENCE_CHARACTERS


class StrictModel(BaseModel):
    """Reject undocumented judge/configuration fields."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class LengthBudget(StrictModel):
    """Language-specific parameters fitted from reviewed concise responses."""

    base: float = Field(ge=0)
    per_nugget: float = Field(gt=0)
    optional_cap: float = Field(ge=0, le=1)


class Calibration(StrictModel):
    """An explicit human-reviewed budget release, embedded in the profile."""

    id: str = Field(min_length=1)
    version: Literal["tau-feedback-calibration/1"] = "tau-feedback-calibration/1"
    length_rule: Literal["nfc_nonwhitespace/1"] = "nfc_nonwhitespace/1"
    reviewed: Literal[True]
    provenance: str = Field(min_length=1)
    review_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description="Hash of the calibration file used to load this profile.",
    )
    languages: dict[str, LengthBudget] = Field(min_length=1)


class FeedbackConfig(StrictModel):
    """Opt-in feedback settings; no fabricated default calibration."""

    language: str | None = None
    calibration: Calibration | None = None
    evidence_max_characters: int = Field(
        default=DEFAULT_FEEDBACK_EVIDENCE_CHARACTERS, ge=256
    )


class Nugget(StrictModel):
    """One independently testable, evidence-grounded communication obligation."""

    id: str = Field(min_length=1)
    proposition: str = Field(min_length=1)
    necessity: Literal["required", "optional"]
    kind: Literal[
        "result",
        "status",
        "reason",
        "limitation",
        "next_step",
        "clarification",
        "required_confirmation",
    ]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    equivalence_group: str = Field(min_length=1)


class NuggetInventory(StrictModel):
    """First-stage verdict, produced without the target assistant transcript."""

    status: Literal["complete", "unavailable", "not_applicable"]
    reason: str
    outcome: Literal[
        "success",
        "partial_success",
        "failed_confirmed",
        "outcome_unknown",
        "blocked_on_user",
        "not_attempted",
        "evidence_incomplete",
    ]
    nuggets: list[Nugget]

    @model_validator(mode="after")
    def complete_inventory(self):
        if self.status == "complete" and not any(
            n.necessity == "required" for n in self.nuggets
        ):
            raise ValueError("Complete inventories need a required obligation")
        if self.status != "complete" and self.nuggets:
            raise ValueError("Incomplete inventories must not contain scored nuggets")
        ids = [n.id for n in self.nuggets]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate nugget IDs")
        return self


class Quote(StrictModel):
    """An exact contiguous passage of actually delivered assistant text."""

    utterance_id: str
    text: str = Field(min_length=1)


class Match(StrictModel):
    """One verdict per frozen nugget."""

    nugget_id: str
    status: Literal["matched", "missing", "contradicted", "uncertain"]
    reason: str = Field(min_length=1)
    quotes: list[Quote]


class Finding(StrictModel):
    """Explanatory repetition or irrelevant expression, without a truth verdict."""

    kind: Literal["repetition", "irrelevant", "uncertain"]
    reason: str = Field(min_length=1)
    quotes: list[Quote] = Field(min_length=1)
    evidence_refs: list[str]


class Alignment(StrictModel):
    """Second-stage labels only; all numbers are calculated by Python."""

    matches: list[Match]
    findings: list[Finding]

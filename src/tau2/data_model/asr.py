"""Private per-call ASR vocabulary and user-visible source material."""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.config import (
    ASR_HOTWORD_BUILDER_VERSION,
    DEFAULT_ASR_HOTWORD_MAX_TERMS,
    DEFAULT_ASR_HOTWORD_WEIGHT,
)

HotwordWeight = Literal[1, 2, 3, 4, 5, 50]


class HotwordSettings(BaseModel):
    """Automatic user-side vocabulary; legacy runs remain opt-in."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    mode: Literal["user_background", "gaia2_stage_gt"] = "user_background"
    weight: HotwordWeight = DEFAULT_ASR_HOTWORD_WEIGHT
    max_terms: int = Field(default=DEFAULT_ASR_HOTWORD_MAX_TERMS, ge=1, le=2000)
    cache_dir: Path | None = None
    catalog_dir: Path | None = None

    @model_validator(mode="after")
    def vocabulary_limits(self):
        """Special weight 50 permits at most 50 terms in each actual request."""
        if (
            self.enabled
            and self.weight == 50
            and self.max_terms > 50
            and self.mode != "gaia2_stage_gt"
        ):
            raise ValueError("ASR super hotwords require max_terms <= 50")
        if self.enabled and self.mode == "gaia2_stage_gt" and self.weight != 50:
            raise ValueError("The fixed Gaia2 stage catalog uses weight 50")
        return self


class HotwordCandidate(BaseModel):
    """One literal entity from an identified piece of user-visible material."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    source: str
    priority: int = 2


class HotwordMaterials(BaseModel):
    """Only material that the user could know, never scoring or reference data."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    candidates: tuple[HotwordCandidate, ...] = ()
    texts: dict[str, str] = Field(default_factory=dict)


class HotwordEntry(BaseModel):
    """A weighted phrase with provenance in a fixed call or stage vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    weight: HotwordWeight
    sources: tuple[str, ...]


class HotwordProfile(BaseModel):
    """An auditable extraction result, independent of the Agent configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = ASR_HOTWORD_BUILDER_VERSION
    entries: tuple[HotwordEntry, ...] = ()
    cache_key: str
    materials_sha256: str
    extractor_model: str
    extractor_args_sha256: str
    cache_hit: bool = False
    rejected_terms: int = 0
    truncated_terms: int = 0
    extraction_usage: dict = Field(default_factory=dict)
    extraction_cost: float | None = None
    extraction_seconds: float | None = None

    @model_validator(mode="after")
    def super_hotword_limit(self):
        """Reject oversized special-weight profiles before making a request."""
        if sum(entry.weight == 50 for entry in self.entries) > 50:
            raise ValueError("ASR permits at most 50 super hotwords")
        return self

    def vocabulary(self) -> dict[str, int]:
        """Return a fresh wire dictionary, so callers cannot mutate the profile."""
        return {entry.text: entry.weight for entry in self.entries}

    @property
    def vocabulary_sha256(self) -> str:
        """Identify exactly the phrases and weights sent to the provider."""
        payload = json.dumps(self.vocabulary(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def record(self) -> dict:
        """Serialize the full actual vocabulary and its extraction provenance."""
        return {
            "enabled": True,
            **self.model_dump(mode="json"),
            "vocabulary_sha256": self.vocabulary_sha256,
            "term_count": len(self.entries),
        }


class StageHotwordVocabulary(BaseModel):
    """A frozen cumulative vocabulary for one released task stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: int = Field(ge=0)
    entries: tuple[HotwordEntry, ...]
    new_terms: tuple[str, ...]
    vocabulary_sha256: str


class StagedHotwordProfile(BaseModel):
    """Evaluator-private fixed catalog; actors never receive this object."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    task_id: str
    input_sha256: str
    prompt_sha256: str
    extractor_model: str
    catalog_sha256: str
    stages: tuple[StageHotwordVocabulary, ...]

    def for_stage(self, stage: int) -> HotwordProfile:
        """Return only the vocabulary allowed by the requested released stage."""
        selected = next((row for row in self.stages if row.stage == stage), None)
        if selected is None:
            raise ValueError(f"No ASR vocabulary for stage {stage}")
        return HotwordProfile(
            version=self.version,
            entries=selected.entries,
            cache_key=f"{self.input_sha256}:stage{stage}",
            materials_sha256=self.input_sha256,
            extractor_model=self.extractor_model,
            extractor_args_sha256=self.catalog_sha256,
            cache_hit=True,
        )

    def vocabulary(self) -> dict[str, int]:
        """Expose only stage zero to callers expecting the older profile API."""
        return self.for_stage(0).vocabulary()

    def record(self) -> dict:
        """Record the selected task catalog without runtime credentials."""
        return {
            "enabled": True,
            "mode": "gaia2_stage_gt",
            **self.model_dump(mode="json"),
        }

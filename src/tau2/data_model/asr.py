"""Private per-call ASR vocabulary and user-visible source material."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from tau2.config import (
    ASR_HOTWORD_BUILDER_VERSION,
    DEFAULT_ASR_HOTWORD_MAX_TERMS,
    DEFAULT_ASR_HOTWORD_WEIGHT,
)


class HotwordSettings(BaseModel):
    """Automatic user-side vocabulary; legacy runs remain opt-in."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    weight: int = Field(default=DEFAULT_ASR_HOTWORD_WEIGHT, ge=1, le=5)
    max_terms: int = Field(default=DEFAULT_ASR_HOTWORD_MAX_TERMS, ge=1, le=2000)
    cache_dir: Path | None = None


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
    """A weighted phrase with provenance, frozen for the entire call."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    weight: int = Field(ge=1, le=5)
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

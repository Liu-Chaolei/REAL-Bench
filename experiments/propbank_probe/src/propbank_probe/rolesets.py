"""Optional roleset lookup without guessing a single predicate sense."""

from __future__ import annotations

from pathlib import Path

import yaml


def load_roleset_index(path: Path | None) -> dict[str, list[str]]:
    """Load a small optional lemma-to-roleset index."""

    if path is None:
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("roleset index must be a mapping")
    return {
        str(lemma): [str(item) for item in values]
        for lemma, values in data.items()
        if isinstance(values, list)
    }


def candidates_for(lemma: str | None, index: dict[str, list[str]]) -> list[str]:
    """Return candidates while preserving zero or many possibilities."""

    return list(index.get(lemma or "", []))


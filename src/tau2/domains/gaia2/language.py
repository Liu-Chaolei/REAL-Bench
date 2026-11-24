"""Gaia2-owned English caller configuration and task metadata."""

from pathlib import Path

import yaml
from pydantic import TypeAdapter

from tau2.data_model.language import LanguagePack, TaskMetadata


def language_root() -> Path:
    """Resolve the Gaia2 language root under the configured data directory."""
    import tau2.utils

    return tau2.utils.DATA_DIR / "tau2/domains/gaia2/language"


def load_pack_bytes(language: str = "en") -> tuple[LanguagePack, bytes]:
    """Read the English pack without falling back to another dataset."""
    if language != "en":
        raise ValueError("Gaia2 supports English calls only")
    payload = (language_root() / "en/pack.yaml").read_bytes()
    pack = LanguagePack.model_validate(yaml.safe_load(payload))
    if pack.language != "en" or set(pack.domains) != {"gaia2"}:
        raise ValueError("Gaia2 pack must bind only en/gaia2")
    binding = pack.binding("gaia2")
    if (
        pack.default_binding is not None
        or binding.canonical_scenario
        or binding.regional_bundle_file is not None
        or binding.tasks_file is not None
        or binding.source != "registry"
    ):
        raise ValueError(
            "Gaia2 tasks are loaded from its registry without regional overrides"
        )
    return pack, payload


def load_metadata() -> dict[str, TaskMetadata]:
    """Read Gaia2's caller metadata without loading any tasks or providers."""
    return TypeAdapter(dict[str, TaskMetadata]).validate_json(
        (language_root() / "en/metadata.json").read_bytes()
    )

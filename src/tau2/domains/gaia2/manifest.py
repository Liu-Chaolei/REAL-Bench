"""Gaia2 release manifest and task-group loading.

The manifest is a content index for Gaia2. It selects task IDs and records the
input digests used by a run; it does not score a task or alter reward logic.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.data_model.tasks import Task
from tau2.domains.gaia2.environment import get_tasks
from tau2.domains.gaia2.utils import GAIA2_DATA_DIR


class Gaia2TaskGroup(BaseModel):
    """One named Gaia2 population in the release inventory."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["main", "interaction_variants", "hidden"]
    count: int = Field(ge=0)
    task_ids: list[str] = Field(min_length=1)
    status: Literal["released", "draft", "experimental"]
    scoring: Literal["primary", "diagnostic", "separate"]

    @model_validator(mode="after")
    def count_matches_ids(self) -> "Gaia2TaskGroup":
        if self.count != len(self.task_ids):
            raise ValueError(f"{self.source} group count does not match task IDs")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("Gaia2 task group contains duplicate task IDs")
        return self


class Gaia2Manifest(BaseModel):
    """Versioned Gaia2 task inventory."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["gaia2-benchmark-v1"]
    benchmark_version: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    domain: Literal["gaia2"]
    modes: list[Literal["text", "voice"]] = Field(min_length=1)
    task_sources: dict[str, dict[str, str]]
    task_groups: dict[str, Gaia2TaskGroup] = Field(min_length=1)
    conditions: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_population_membership(self) -> "Gaia2Manifest":
        seen: set[str] = set()
        for name, group in self.task_groups.items():
            overlap = seen.intersection(group.task_ids)
            if overlap and name not in {"base"}:
                raise ValueError(f"Gaia2 task groups overlap: {sorted(overlap)[:3]}")
            if name != "base":
                seen.update(group.task_ids)
        return self


def manifest_path(root: Path = GAIA2_DATA_DIR) -> Path:
    """Resolve the Gaia2 release manifest inside the domain data directory."""

    return Path(root).parents[1] / "benchmarks" / "gaia2.json"


def load_manifest(path: Path | None = None) -> Gaia2Manifest:
    """Load and validate the Gaia2 release inventory."""

    path = path or manifest_path()
    return Gaia2Manifest.model_validate_json(Path(path).read_text())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest_sources(
    manifest: Gaia2Manifest, *, root: Path = GAIA2_DATA_DIR
) -> None:
    """Check source bytes and task IDs before constructing a run."""

    root = Path(root)
    for source in manifest.task_sources.values():
        relative = source["file"]
        path = (root / relative).resolve()
        allowed_external = root.parent / "gaia2_hidden"
        if not path.is_relative_to(root.resolve()) and not path.is_relative_to(
            allowed_external.resolve()
        ):
            raise ValueError("Gaia2 manifest source escapes the data directory")
        if not path.is_file():
            raise FileNotFoundError(path)
        expected = source.get("sha256")
        if expected and _sha256(path) != expected:
            raise ValueError(f"Gaia2 source digest changed: {path}")

    main = {
        task.id for task in get_tasks(root=root, include_interaction_variants=False)
    }
    variants_path = root / "interaction_variants.json"
    variants = (
        {row["id"] for row in json.loads(variants_path.read_text())}
        if variants_path.exists()
        else set()
    )
    hidden_path = root.parent / "gaia2_hidden" / "tasks.json"
    hidden = (
        {row["id"] for row in json.loads(hidden_path.read_text())}
        if hidden_path.exists()
        else set()
    )
    available = main | variants | hidden
    for name, group in manifest.task_groups.items():
        missing = sorted(set(group.task_ids) - available)
        if missing:
            raise ValueError(
                f"Gaia2 group {name!r} contains unknown task IDs: {missing[:3]}"
            )


def load_task_group(
    name: str, *, manifest: Gaia2Manifest | None = None, root: Path = GAIA2_DATA_DIR
) -> list[Task]:
    """Load one manifest group using the ordinary domain task loaders."""

    manifest = manifest or load_manifest()
    verify_manifest_sources(manifest, root=root)
    group = manifest.task_groups.get(name)
    if group is None:
        raise KeyError(f"Unknown Gaia2 task group: {name}")
    if group.source == "hidden":
        from tau2.domains.gaia2_hidden.loader import get_tasks as get_hidden_tasks

        tasks = get_hidden_tasks(task_split_name="hidden_intent")
    else:
        tasks = get_tasks(root=root, include_interaction_variants=True)
    index = {task.id: task for task in tasks}
    return [index[task_id] for task_id in group.task_ids]


def write_manifest(manifest: Gaia2Manifest, path: Path | None = None) -> Path:
    """Write a validated manifest atomically enough for local authoring."""

    path = path or manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    return path

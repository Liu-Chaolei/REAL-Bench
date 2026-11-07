"""Atomic work records and immutable source identity checks."""

import hashlib
import json
import random
import tempfile
from pathlib import Path

from tau2.data_model.tasks import Task

CATEGORIES = ("search", "execution", "ambiguity", "adaptability", "time")


def digest(value) -> str:
    """Hash JSON-compatible content, including native Pydantic records."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    """Hash bytes without loading large assets into memory."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    """Publish a record atomically; each worker owns a different record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".writing",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    temporary.replace(path)


def write_jsonl(path: Path, values) -> None:
    """Publish a deterministic JSON-lines collection atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".writing",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def record_path(dataset: Path, task_id: str) -> Path:
    """Use a digest as filename; task IDs can never introduce path traversal."""
    return Path(dataset) / "work" / f"{digest(task_id)}.json"


def read_record(dataset: Path, task_id: str) -> dict:
    """Load a work record or return an empty, unstarted record."""
    path = record_path(dataset, task_id)
    return json.loads(path.read_text()) if path.exists() else {"base_task_id": task_id}


def source_tasks(dataset: Path) -> tuple[dict, dict[str, Task]]:
    """Check source bytes and runtime on every command before using them."""
    from tau2.domains.gaia2.utils import runtime_digest, verify_task_artifact

    manifest = json.loads((Path(dataset) / "manifest.json").read_text())
    root = Path(manifest["source_root"])
    for name, expected in manifest["source_files"].items():
        if file_digest(root / name) != expected:
            raise ValueError(f"Source changed since inventory: {name}")
    if runtime_digest() != manifest["runtime_sha256"]:
        raise ValueError("Gaia2 runtime changed since inventory")
    verify_task_artifact(root)
    from tau2.domains.gaia2.verify import verify_pinned_resources

    source_manifest = json.loads((root / "manifest.json").read_text())
    verify_pinned_resources(root, source_manifest["assets_sha256"])
    tasks = {
        value["id"]: Task.model_validate(value)
        for value in json.loads((root / "tasks.json").read_text())
    }
    if len(tasks) != manifest["expected_tasks"]:
        raise ValueError("Source task population changed")
    return manifest, tasks


def stratified_ids(
    tasks: dict[str, Task], per_category: int, seed: int, exclude=()
) -> list[str]:
    """Select fixed category-balanced samples, independent of execution order."""
    rng = random.Random(seed)
    selected = []
    excluded = set(exclude)
    for category in CATEGORIES:
        population = sorted(
            t.id
            for t in tasks.values()
            if t.scenario.category == category and t.id not in excluded
        )
        if len(population) < per_category:
            raise ValueError(
                f"Not enough {category} tasks for a sample of {per_category}"
            )
        selected.extend(rng.sample(population, per_category))
    return sorted(selected)

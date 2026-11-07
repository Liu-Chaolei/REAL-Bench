"""Immutable historical data archives for strict trajectory re-evaluation."""

import hashlib
import json
import shutil
from pathlib import Path

from pydantic import TypeAdapter

from tau2.data_model.language import TaskMetadata
from tau2.domains.gaia2.utils import verify_task_artifact
from tau2.multilingual.runtime import _canonical_json, task_digest, task_payload


def archive_data(source, destination):
    """Copy task/snapshot and language pins before resealing the live population."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(source, destination)
    repository_data = source.parents[1]
    shutil.copytree(
        repository_data / "multilingual_extensions/gaia2",
        destination / "language_extension",
    )
    shutil.copyfile(
        repository_data / "multilingual/en/pack.yaml", destination / "pack.yaml"
    )
    hashes = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in destination.rglob("*")
        if p.is_file()
    }
    (destination / "archive.json").write_text(
        json.dumps({"files": hashes}, indent=2) + "\n"
    )
    return destination


def validate_recorded_data(results, root):
    """Verify archived tasks and all recorded language/task artifact bindings."""
    root = Path(root)
    if results.info.environment_info.domain_name != "gaia2":
        raise ValueError("recorded-data-root is supported only for Gaia2 archives")
    archive = json.loads((root / "archive.json").read_text())
    for relative, expected in archive["files"].items():
        path = (root / relative).resolve()
        if (
            not path.is_relative_to(root.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Recorded archive differs from its integrity manifest")
    verify_task_artifact(root)
    from tau2.domains.gaia2.environment import get_tasks

    tasks = get_tasks(root=root)
    index = {t.id: t for t in tasks}
    for task in results.tasks:
        verify_task_artifact(root, task)
        if task_digest(task) != task_digest(index[task.id]):
            raise ValueError("Embedded task differs from recorded data")
    metadata = TypeAdapter(dict[str, TaskMetadata]).validate_json(
        (root / "language_extension/en/metadata.json").read_bytes()
    )
    tasks_hash = hashlib.sha256(
        _canonical_json([task_payload(t) for t in tasks])
    ).hexdigest()
    metadata_hash = hashlib.sha256(
        TypeAdapter(dict[str, TaskMetadata]).dump_json(metadata)
    ).hexdigest()
    pack_hash = hashlib.sha256(
        (root / "pack.yaml").read_bytes()
        + (root / "language_extension/en/extension.json").read_bytes()
    ).hexdigest()
    pins = results.info.language_artifacts
    if pins is not None and (
        pins.tasks_sha256 != tasks_hash
        or pins.metadata_sha256 != metadata_hash
        or pins.pack_sha256 != pack_hash
    ):
        raise ValueError("Recorded language artifact pins differ from archived data")
    for identifier, context in results.info.language_tasks.items():
        if (
            context.task_sha256 != task_digest(index[identifier])
            or context.artifact_pins != pins
        ):
            raise ValueError(
                "Recorded language task binding differs from archived data"
            )
    for simulation in results.simulations:
        context = results.info.language_tasks.get(simulation.task_id)
        if (
            simulation.language_context is not None
            and simulation.language_context != context
        ):
            raise ValueError(
                "Simulation language binding differs from its recorded task"
            )
    return root

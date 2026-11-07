"""Content-addressed material indexes and bounded, traceable retrieval."""

import json
import math
import re
from collections import Counter
from pathlib import Path

from tau2.config import DEFAULT_SEED
from tau2.data_model.tasks import Task
from tau2.task_authoring.gaia2_series import VERSION
from tau2.task_authoring.gaia2_series.schema import validate_json_pointer
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    source_tasks,
    stratified_ids,
    write_json,
    write_jsonl,
)


def pointer_get(value, pointer: str):
    """Read an RFC 6901 pointer; absence is an error, never empty material."""
    validate_json_pointer(pointer)
    if pointer == "":
        return value
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def escape_pointer(value) -> str:
    """Encode a dictionary key as one JSON pointer component."""
    return str(value).replace("~", "~0").replace("/", "~1")


def app_states(snapshot: list[dict]) -> dict:
    """Address initial material by application name rather than array ordinal."""
    return {app["name"]: app["app_state"] for app in snapshot}


def index_records(value, pointer=""):
    """Index bounded records with their exact paths, without truncating facts."""
    if isinstance(value, dict):
        scalars = {k: v for k, v in value.items() if not isinstance(v, (dict, list))}
        if scalars:
            text = json.dumps(scalars, ensure_ascii=False)
            if len(text) <= 12000:
                yield {"pointer": pointer, "value": scalars}
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from index_records(child, pointer + "/" + escape_pointer(key))
    elif isinstance(value, list):
        for number, child in enumerate(value):
            yield from index_records(child, pointer + f"/{number}")


def public_schema(tools) -> dict:
    """Use actual public tool schemas; private environment methods stay private."""
    return {name: tool.openai_schema for name, tool in tools.get_tools().items()}


def inventory(source: Path, dataset: Path, seed: int = DEFAULT_SEED) -> dict:
    """Freeze all source identities and cache indexes for the 24 shared snapshots."""
    from tau2.domains.gaia2.environment import get_tasks
    from tau2.domains.gaia2.tools import Gaia2Tools
    from tau2.domains.gaia2.utils import read_snapshot, runtime_digest
    from tau2.domains.gaia2.verify import verify_pinned_resources

    source, dataset = Path(source).resolve(), Path(dataset).resolve()
    if dataset.is_relative_to(source) or source.is_relative_to(dataset):
        raise ValueError("Authoring output must be separate from the sealed source")
    original = json.loads((source / "manifest.json").read_text())
    if not original.get("verified") or original["runtime_sha256"] != runtime_digest():
        raise ValueError(
            "A sealed source matching the current Gaia2 runtime is required"
        )
    verify_pinned_resources(source, original["assets_sha256"])
    tasks = {task.id: task for task in get_tasks(root=source)}
    if len(tasks) != 800:
        raise ValueError("Inventory requires all 800 original tasks")
    manifest = {
        "version": VERSION,
        "source_root": str(source),
        "expected_tasks": 800,
        "runtime_sha256": runtime_digest(),
        "seed": seed,
        "source_files": {
            name: file_digest(source / name)
            for name in [
                "tasks.json",
                "manifest.json",
                "resources.json",
                "split_tasks.json",
            ]
        },
        "pilot_ids": stratified_ids(tasks, 10, seed),
        "status": "authoring",
        "execution_ready": False,
    }
    manifest["additional_human_ids"] = stratified_ids(
        tasks, 15, seed, exclude=manifest["pilot_ids"]
    )
    path = dataset / "manifest.json"
    if path.exists():
        old = json.loads(path.read_text())
        if {key: old.get(key) for key in manifest} != manifest:
            raise ValueError("Existing inventory differs; use a new output directory")
        source_tasks(dataset)
        return old
    dataset.mkdir(parents=True, exist_ok=True)
    for snapshot_id in sorted({task.scenario.snapshot for task in tasks.values()}):
        snapshot = read_snapshot(snapshot_id, source)
        toolkit = Gaia2Tools(snapshot, assets=Path(original["assets"]))
        try:
            schemas = public_schema(toolkit)
        finally:
            toolkit.close()
        visible_apps = {name.split("__", 1)[0] for name in schemas}
        material = {
            name: value
            for name, value in app_states(snapshot).items()
            if name in visible_apps
        }
        write_json(
            dataset / "indexes" / f"{snapshot_id}.json",
            {
                "snapshot": snapshot_id,
                "schemas": schemas,
                "records": list(index_records(material)),
            },
        )
    write_jsonl(
        dataset / "inventory.jsonl",
        (
            {
                "id": task.id,
                "sha256": digest(task),
                "category": task.scenario.category,
                "snapshot": task.scenario.snapshot,
                "original_user_events": sum(
                    e.kind == "user" for e in task.scenario.events
                ),
            }
            for task in tasks.values()
        ),
    )
    write_json(path, manifest)
    return manifest


def context_packet(dataset: Path, base: Task, state: dict, limit: int) -> dict:
    """Retrieve relevant full records and advertise how to request omitted data."""
    index = json.loads(
        (Path(dataset) / "indexes" / f"{base.scenario.snapshot}.json").read_text()
    )
    instructions = base.user_scenario.instructions
    query = json.dumps(
        {
            "request": instructions
            if isinstance(instructions, str)
            else instructions.reason_for_call,
            "reference_arguments": [
                event.arguments
                for event in base.scenario.events
                if event.kind == "reference"
            ],
        },
        ensure_ascii=False,
    )
    terms = set(re.findall(r"[\w@.-]{4,}", query.casefold()))
    visible = {name.split("__", 1)[0] for name in index["schemas"]}
    records = list(
        index_records({name: value for name, value in state.items() if name in visible})
    )
    tokens = {
        row["pointer"]: set(re.findall(r"[\w@.-]{4,}", json.dumps(row).casefold()))
        for row in records
    }
    frequency = Counter(word for words in tokens.values() for word in words)
    ranked = sorted(
        records,
        key=lambda row: (
            -sum(
                math.log(1 + len(records) / (1 + frequency[word]))
                for word in terms & tokens[row["pointer"]]
            ),
            row["pointer"],
        ),
    )
    selected, size = [], 0
    for row in ranked:
        length = len(json.dumps(row, ensure_ascii=False))
        if size + length <= limit:
            selected.append(row)
            size += length
    return {
        "tool_names": sorted(index["schemas"]),
        "material_records": selected,
        "omitted_records": len(records) - len(selected),
        "retrieval": "Use get_material for complete JSON records and get_tool_schema for public tool signatures.",
    }

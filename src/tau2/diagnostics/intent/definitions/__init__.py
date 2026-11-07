"""Shared definitions shipped in the wheel, independent of example directories."""

import hashlib
import json
from importlib.resources import files
from typing import Any


def load_definition(name: str) -> dict[str, Any]:
    if name != "gaia2":
        raise ValueError(f"Unknown intent definition set: {name}")
    root = files(__package__).joinpath(*name.split("/"))
    manifest = json.loads(root.joinpath("definition.json").read_text())
    content = {}
    for filename, expected in manifest["sha256"].items():
        raw = root.joinpath(filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Definition resource hash mismatch: {filename}")
        content[filename] = raw.decode()
    return {
        "manifest": manifest,
        "schema": json.loads(content["intent.schema.json"]),
        "instructions": content["extraction_instructions.md"],
        "validation": json.loads(content["validation.profile.json"]),
        "scoring": json.loads(content["scoring.profile.json"]),
    }

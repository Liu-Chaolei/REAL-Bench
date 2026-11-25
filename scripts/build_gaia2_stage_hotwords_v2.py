"""Install the reviewed Gaia2 V2 catalog for the current 1000-task population.

The V2 profiles are copied from the reviewed tau-backup artifact, keeping only
task IDs present in the current Gaia2 tasks file. The manifest is resealed for
those profile bytes and the current task population. This script performs no
model calls and never creates standalone async profiles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def build(source_ref: str, tasks_path: Path, output: Path) -> None:
    tasks = json.loads(tasks_path.read_text())
    output_tasks = output / "tasks"
    output_tasks.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(
        subprocess.check_output(
            ["git", "show", f"{source_ref}:data/gaia2_stage_hotwords/v2/manifest.json"]
        )
    )
    manifest["tasks"] = len(tasks)
    manifest["original_tasks"] = len(tasks)
    manifest["async_tasks"] = 0
    manifest["dataset_sha256"] = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    profile_hashes = {}
    for task in tasks:
        task_id = task["id"]
        raw = subprocess.check_output(
            [
                "git",
                "show",
                f"{source_ref}:data/gaia2_stage_hotwords/v2/tasks/{task_id}.json",
            ]
        )
        profile = json.loads(raw)
        path = output_tasks / f"{task_id}.json"
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
        profile_hashes[task_id] = canonical_digest(profile)
    manifest["profiles_sha256"] = profile_hashes
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ref", default="origin/tau-backup")
    parser.add_argument(
        "--tasks", type=Path, default=Path("data/tau2/domains/gaia2/tasks.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/gaia2_stage_hotwords/v2")
    )
    args = parser.parse_args()
    build(args.source_ref, args.tasks, args.output)


if __name__ == "__main__":
    main()

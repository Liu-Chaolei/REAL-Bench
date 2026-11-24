"""Build the current 1000-task Gaia2 stage catalog from reviewed profiles.

This migration helper retains only phrases from an existing reviewed catalog
that remain literal substrings of the current task's stage material. It never
calls a model and never creates profiles for standalone async task IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from tau2.data_model.tasks import Task
from tau2.user.stage_asr_hotwords import stage_hotword_input


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def build(source: Path, tasks_path: Path, output: Path) -> None:
    tasks = json.loads(tasks_path.read_text())
    output_tasks = output / "tasks"
    output_tasks.mkdir(parents=True, exist_ok=True)
    for raw in tasks:
        task = Task.model_validate(raw)
        material = stage_hotword_input(task)
        reviewed = json.loads((source / "tasks" / f"{task.id}.json").read_text())
        active: dict[str, dict] = {}
        stages = []
        for supplied, current in zip(reviewed["stages"], material["stages"], strict=True):
            extracted = []
            previous = set(active)
            for term in supplied.get("extracted", []):
                phrase = " ".join(str(term.get("text", "")).split())
                if not phrase or "@" in phrase or not any(c.isalpha() for c in phrase):
                    continue
                matches = [
                    path for path, text in current["sources"].items()
                    if phrase in " ".join(str(text).split())
                ]
                if not matches or phrase.casefold() in {x.casefold() for x in extracted}:
                    continue
                source_name = matches[0]
                extracted.append({"text": phrase, "source": source_name})
                active.setdefault(
                    phrase.casefold(), {"text": phrase, "source": source_name}
                )
            vocabulary = {row["text"]: 50 for row in active.values()}
            stages.append(
                {
                    "stage": current["stage"],
                    "extracted": extracted,
                    "new_terms": [
                        row["text"] for key, row in active.items() if key not in previous
                    ],
                    "active_vocabulary": vocabulary,
                    "vocabulary_sha256": digest(vocabulary),
                }
            )
        profile = {
            "version": "gaia2-stage-hotwords-v3",
            "task_id": task.id,
            "model": "deepseek-flash",
            "input_sha256": digest(material),
            "prompt_sha256": "6b16a6b205ac26bd203d0dcf9c8bf40fc87be6bfc0e93124bbdce5a6da5e457c",
            "weight": 50,
            "stages": stages,
        }
        (output_tasks / f"{task.id}.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n"
        )
    manifest = {
        "version": "gaia2-stage-hotwords-v3",
        "tasks": len(tasks),
        "async_tasks": 0,
        "dataset_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
        "prompt_sha256": "6b16a6b205ac26bd203d0dcf9c8bf40fc87be6bfc0e93124bbdce5a6da5e457c",
        "model": "deepseek-flash",
        "weight": 50,
        "weight_policy": "first-50-cumulative-v1",
        "complete": True,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--tasks", type=Path, default=Path("data/tau2/domains/gaia2/tasks.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/gaia2_stage_hotwords/v3")
    )
    args = parser.parse_args()
    build(args.source, args.tasks, args.output)


if __name__ == "__main__":
    main()

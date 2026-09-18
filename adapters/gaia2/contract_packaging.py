"""Build official task contracts in the pinned offline ARE image."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml


def build_contracts(dataset, *, verify=False, protocol="gaia2-qwen-audio/5"):
    from adapters.gaia2.qwen_audio_template.environment.runner.action_decision import (
        ARE_IMAGE,
    )

    runner = Path(__file__).parent / "qwen_audio_template/environment/runner"
    command = [
        "docker",
        "run",
        "--rm",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--network",
        "none",
        "-v",
        f"{runner.resolve()}:/opt/runner:ro",
        "-v",
        f"{dataset.resolve()}:/dataset:{'ro' if verify else 'rw'}",
        ARE_IMAGE
        if protocol in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}
        else "hearme-gaia2-are:dev",
        "python",
        "/opt/runner/task_contract.py",
        "--dataset",
        "/dataset",
        "--protocol",
        protocol,
    ]
    if verify:
        command.append("--verify")
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=600, check=False
    )
    if completed.returncode:
        raise ValueError(
            "Official contract generation failed: " + completed.stderr[-4000:]
        )
    if not verify:
        for compose in dataset.glob("*/environment/docker-compose.yaml"):
            data = yaml.safe_load(compose.read_text())
            volumes = data["services"]["are-runner"].setdefault("volumes", [])
            mount = (
                "${CONTEXT_DIR}/task-contract.json:/evaluation/task-contract.json:ro"
            )
            if mount not in volumes:
                volumes.append(mount)
                compose.write_text(yaml.safe_dump(data, sort_keys=False))
    return {
        source.parent.parent.name: json.loads(
            source.with_name("task-contract.json").read_text()
        )
        for source in dataset.glob("*/environment/scenario.json")
    }

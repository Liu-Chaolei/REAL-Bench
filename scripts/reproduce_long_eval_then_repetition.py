#!/usr/bin/env python3
"""Replay the original long evaluation request followed by repetition."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tau2.diagnostics.config import MetricsConfig  # noqa: E402
from tau2.diagnostics.model import RequestBudget, TauJsonModel  # noqa: E402
from tau2.utils.env_config import (  # noqa: E402
    read_config_environment,
    resolve_env_reference,
)


def replay_module():
    path = ROOT / "scripts/reproduce_diagnostics_litellm.py"
    spec = importlib.util.spec_from_file_location("repro", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main():
    os.environ["DEEPSEEK_API_KEY"] = resolve_env_reference(
        read_config_environment(
            "/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env"
        ),
        "DEEPSEEK_API_KEY",
    )
    config = MetricsConfig.load(ROOT / "examples/real-metrics.deepseek.yaml")
    budget = RequestBudget(10)
    cache = (
        ROOT / "data/simulations/backup-validation-20261111/litellm-long-repro-cache-2"
    )
    cache.mkdir(parents=True, exist_ok=True)
    debug = next(
        (ROOT / "data/simulations/backup-validation-20261111/live-complex-0446").glob(
            "artifacts/*/sim_*/llm_debug/*diagnostic_evaluation*.json"
        )
    )
    evaluation_prompt = json.loads(debug.read_text())["request"]["messages"][0][
        "content"
    ]
    # llm_utils debug logging splits newline-delimited content into a list.
    if isinstance(evaluation_prompt, list):
        evaluation_prompt = "\n".join(evaluation_prompt)
    repetition_prompt = replay_module().repetition_prompt(
        ROOT / "data/simulations/backup-validation-20261111/live-complex-0446"
    )
    results = {
        "sequence": [],
        "evaluation_prompt_characters": len(evaluation_prompt),
        "repetition_prompt_characters": len(repetition_prompt),
    }
    for name, role, prompt in (
        ("original_evaluation", "evaluation", evaluation_prompt),
        ("repetition_after_evaluation", "evaluation", repetition_prompt),
    ):
        print(f"START {name} chars={len(prompt)}", flush=True)
        started = time.time()
        row = {"name": name, "role": role, "prompt_characters": len(prompt)}
        try:
            model = TauJsonModel(config, role, cache / name, budget)
            output = model.complete_sync(prompt, None)
            row.update(
                status="returned",
                elapsed_seconds=time.time() - started,
                output_characters=len(output),
            )
            print(f"DONE {name} elapsed={row['elapsed_seconds']:.1f}s", flush=True)
        except Exception as exc:
            row.update(
                status="error",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                elapsed_seconds=time.time() - started,
            )
            print(
                f"ERROR {name} {type(exc).__name__} elapsed={row['elapsed_seconds']:.1f}s",
                flush=True,
            )
        results["sequence"].append(row)
        (
            ROOT / "data/simulations/backup-validation-20261111/litellm-long-repro.json"
        ).write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()

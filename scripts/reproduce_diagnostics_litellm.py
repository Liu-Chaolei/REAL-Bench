#!/usr/bin/env python3
"""Reproduce the saved diagnostics requests through tau2's LiteLLM path."""

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
from tau2.diagnostics.intent.call import combined_sources  # noqa: E402
from tau2.diagnostics.intent.config import IntentConfig  # noqa: E402
from tau2.diagnostics.intent.extraction import (  # noqa: E402
    EXTRACTION_POLICY,
    extraction_schema,
)
from tau2.diagnostics.intent.io import canonical  # noqa: E402
from tau2.diagnostics.intent.repetition import (  # noqa: E402
    REPETITION_POLICY,
    RepetitionJudgment,
    goal_paths,
    raw_input,
)
from tau2.diagnostics.model import RequestBudget, TauJsonModel  # noqa: E402
from tau2.diagnostics.suite import load_evidence  # noqa: E402
from tau2.utils.env_config import (  # noqa: E402
    read_config_environment,
    resolve_env_reference,
)


def replay_module():
    spec = importlib.util.spec_from_file_location(
        "replay_deepseek_stuck_prompts",
        ROOT / "scripts/replay_deepseek_stuck_prompts.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def prediction_prompt(run_root: Path) -> str:
    evidence_dir = next(run_root.glob("artifacts/task_*/sim_*/diagnostics/evidence"))
    evidence = load_evidence(evidence_dir)
    _user, backend, reason = combined_sources(evidence)
    if reason or backend is None:
        raise RuntimeError(reason)
    definition = IntentConfig(definition_set="gaia2")
    schema, _profile, _validation = (
        definition.load_schema(),
        definition.profile(),
        definition.validation(),
    )
    schema = json.loads(json.dumps(schema))
    schema["$defs"]["temporal_anchor"]["oneOf"][0].setdefault("properties", {})[
        "request_source"
    ] = {
        "type": "string",
        "description": "Source of the request whose time anchors this relative requirement; spelling is not scored.",
    }
    instructions = definition.instructions() + "\n" + backend.context["policy"]
    prompt = (
        EXTRACTION_POLICY
        + "\n"
        + """When input context declares backend_input_scope=initial_request,
the evaluator has selected all backend submissions attributable to the initial
user request, before subsequent user clarification. All selected messages jointly
define the request being evaluated; do not treat earlier selected messages as
irrelevant history merely because another selected message comes last. Keep their
boundaries and resolve explicit corrections within this selected scope. Preserve
business requirements in attached context when they belong to that request.
Embedded routing instructions about the 'current' message do not change this
evaluation scope. Do not infer excluded later user turns or repair missing intent."""
        + "\nMode: prediction\nCommon instructions:\n"
        + instructions
        + "\nInput:\n"
        + canonical({"text": backend.text, "context": backend.context})
    )
    return prompt + "\nOutput JSON schema:\n" + canonical(extraction_schema(schema))


def repetition_prompt(run_root: Path) -> str:
    intent_dir = next(
        run_root.glob("artifacts/task_*/sim_*/diagnostics/evaluations/*/intent")
    )
    gt = json.loads((intent_dir / "gt.json").read_text())
    prediction = json.loads((intent_dir / "prediction.json").read_text())
    payload = {
        "user_input": raw_input(gt),
        "backend_input": raw_input(prediction),
        "prediction_intent": prediction.get("intent"),
        "goal_evidence": prediction.get("evidence", []),
        "goal_paths": goal_paths(prediction.get("intent") or {}),
    }
    return (
        REPETITION_POLICY
        + "\nEvidence:\n"
        + canonical(payload)
        + (
            "\nOutput JSON schema:\n"
            + canonical(RepetitionJudgment.model_json_schema())
        )
    )


def main():
    env_values = read_config_environment(
        "/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env"
    )
    os.environ["DEEPSEEK_API_KEY"] = resolve_env_reference(
        env_values, "DEEPSEEK_API_KEY"
    )
    config = MetricsConfig.load(ROOT / "examples/real-metrics.deepseek.yaml")
    cache = ROOT / "data/simulations/backup-validation-20261111/litellm-repro-cache"
    cache.mkdir(parents=True, exist_ok=True)
    budget = RequestBudget(10)
    cases = [
        (
            "0339_prediction_litellm",
            prediction_prompt(
                ROOT
                / "data/simulations/backup-validation-20261111/live-complex-0339-fixed"
            ),
            "prediction",
        ),
        (
            "0446_repetition_litellm",
            repetition_prompt(
                ROOT / "data/simulations/backup-validation-20261111/live-complex-0446"
            ),
            "evaluation",
        ),
    ]
    output = ROOT / "data/simulations/backup-validation-20261111/litellm-repro.json"
    results = {
        "path": "TauJsonModel.complete_sync -> llm_utils.generate -> LiteLLM",
        "requests": [],
    }
    for name, prompt, role in cases:
        print(f"START {name} chars={len(prompt)}", flush=True)
        started = time.time()
        row = {"name": name, "role": role, "prompt_characters": len(prompt)}
        try:
            model = TauJsonModel(config, role, cache / name, budget)
            value = model.complete_sync(prompt, None)
            row.update(
                status="returned",
                elapsed_seconds=time.time() - started,
                output_characters=len(value),
            )
            print(f"DONE {name} elapsed={row['elapsed_seconds']:.1f}s", flush=True)
        except Exception as exc:
            row.update(
                status="error",
                elapsed_seconds=time.time() - started,
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            print(
                f"ERROR {name} {type(exc).__name__} elapsed={row['elapsed_seconds']:.1f}s",
                flush=True,
            )
        results["requests"].append(row)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

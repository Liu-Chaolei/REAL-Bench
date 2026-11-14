#!/usr/bin/env python3
"""Replay two saved diagnostics prompts sequentially against DeepSeek."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tau2.diagnostics.intent.call import (  # noqa: E402
    combined_sources,
)
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
from tau2.diagnostics.suite import load_evidence  # noqa: E402
from tau2.utils.env_config import (  # noqa: E402
    read_config_environment,
    resolve_env_reference,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def load_backend_extraction_prompt(run_root: Path) -> tuple[str, dict]:
    evidence_dir = next(run_root.glob("artifacts/task_*/sim_*/diagnostics/evidence"))
    evidence = load_evidence(evidence_dir)
    _user, backend, reason = combined_sources(evidence)
    if reason or backend is None:
        raise RuntimeError(f"cannot reconstruct backend input: {reason}")
    definition = IntentConfig(definition_set="gaia2")
    schema = definition.load_schema()
    schema = json.loads(json.dumps(schema))
    anchor = schema["$defs"]["temporal_anchor"]["oneOf"][0]
    anchor.setdefault("properties", {})["request_source"] = {
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
    schema = extraction_schema(schema)
    full = prompt + "\nOutput JSON schema:\n" + canonical(schema)
    return full, {
        "name": "0339_prediction_reconstructed",
        "prompt_sha256": sha(full),
        "prompt_characters": len(full),
        "schema_characters": len(canonical(schema)),
        "evidence_dir": str(evidence_dir),
    }


def load_repetition_prompt(run_root: Path) -> tuple[str, dict]:
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
    schema = RepetitionJudgment.model_json_schema()
    prompt = REPETITION_POLICY + "\nEvidence:\n" + canonical(payload)
    full = prompt + "\nOutput JSON schema:\n" + canonical(schema)
    return full, {
        "name": "0446_repetition_reconstructed",
        "prompt_sha256": sha(full),
        "prompt_characters": len(full),
        "schema_characters": len(canonical(schema)),
        "intent_dir": str(intent_dir),
    }


def resolve_ds() -> tuple[str, str]:
    config = json.loads(
        (
            ROOT
            / "data/simulations/backup-validation-20261111/config/qwen-complex-0446.json"
        ).read_text()
    )
    values = read_config_environment(config["config_env_file"])
    key = resolve_env_reference(values, "DEEPSEEK_API_KEY")
    base = resolve_env_reference(values, "DEEPSEEK_BASE_URL").rstrip("/")
    return key, base


def main() -> None:
    key, base = resolve_ds()
    prompts = [
        load_backend_extraction_prompt(
            ROOT / "data/simulations/backup-validation-20261111/live-complex-0339-fixed"
        ),
        load_repetition_prompt(
            ROOT / "data/simulations/backup-validation-20261111/live-complex-0446"
        ),
    ]
    output = (
        ROOT
        / "data/simulations/backup-validation-20261111/deepseek-stuck-prompt-replay.json"
    )
    results = {"endpoint": base, "model": "deepseek-flash", "requests": []}
    timeout = httpx.Timeout(connect=20.0, read=180.0, write=30.0, pool=20.0)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for prompt, meta in prompts:
            started = time.time()
            row = dict(
                meta,
                started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            )
            print(f"START {meta['name']} chars={meta['prompt_characters']}", flush=True)
            try:
                response = client.post(
                    base + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "model": "deepseek-flash",
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                        "reasoning_effort": "low",
                        "stream": False,
                    },
                )
                elapsed = time.time() - started
                body = response.text
                row.update(
                    status="returned",
                    http_status=response.status_code,
                    elapsed_seconds=elapsed,
                    response_characters=len(body),
                    response_sha256=sha(body),
                    response_preview=body[:240].replace("\n", " "),
                )
                print(
                    f"DONE {meta['name']} status={response.status_code} elapsed={elapsed:.1f}s chars={len(body)}",
                    flush=True,
                )
            except Exception as exc:
                elapsed = time.time() - started
                row.update(
                    status="error",
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    elapsed_seconds=elapsed,
                )
                print(
                    f"ERROR {meta['name']} type={type(exc).__name__} elapsed={elapsed:.1f}s",
                    flush=True,
                )
            results["requests"].append(row)
            output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

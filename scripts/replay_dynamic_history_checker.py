"""Replay saved dynamic requests through the production semantic checker.

The candidate utterances come from earlier real runs.  The request-generation
call is replaced with each saved candidate, while the checker call is the same
``generate_dynamic_followup`` path used at runtime.  This isolates checker
behavior from a new candidate-generation sample.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import SimpleNamespace

from experiment_dynamic_goal_graph import (
    configured_cases,
    load_request_case,
    load_task,
    tool_gt_for_stage,
)

from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.data_model.message import AssistantMessage
from tau2.user import dynamic_followup as dynamic_module
from tau2.user.dynamic_followup import generate_dynamic_followup
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/experiments/dynamic_history_checker_replay_20261118.json"


def _messages(history: str) -> list[SimpleNamespace]:
    result = []
    for line in history.splitlines():
        if ": " not in line:
            continue
        role, content = line.split(": ", 1)
        if role in {"system", "user", "assistant"} and content:
            result.append(SimpleNamespace(role=role, content=content))
    return result


def _candidate(raw_path: Path) -> dict:
    raw = json.loads(raw_path.read_text())
    return json.loads(raw["response"]["content"])


def _safe_error(error: Exception) -> str:
    # Do not put API credentials or complete prompts into the report.
    text = str(error)
    text = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]\s*\S+", r"\1=[REDACTED]", text
    )
    return text[:2000]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT
        / "integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json",
    )
    parser.add_argument(
        "--simulations-root", type=Path, default=ROOT / "data/simulations"
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    config = QwenAgentConfig.model_validate_json(args.config.read_text())
    settings = config.user_llm_settings()
    if settings is None:
        raise RuntimeError("the replay config has no user LLM")
    model, model_args = settings
    model_args = {**model_args, "num_retries": 0, "temperature": 0}
    replay_config = DynamicFollowupConfig(enabled=True, stages=[1, 2], max_retries=0)
    results = {
        "experiment": "production_dynamic_history_checker_replay",
        "model": model,
        "cases": [],
    }

    original_generate = dynamic_module.generate
    for case in configured_cases(args.simulations_root):
        data = load_request_case(case)
        candidate = _candidate(case["request_log"])
        checker_payload = {}

        def replay_generate(**kwargs):
            if kwargs["call_name"] == "dynamic_followup_request":
                return AssistantMessage(
                    role="assistant",
                    content=json.dumps(candidate, ensure_ascii=False),
                )
            reply = original_generate(**kwargs)
            if kwargs["call_name"] == "dynamic_followup_check":
                try:
                    checker_payload.update(json.loads(reply.content or "{}"))
                except json.JSONDecodeError:
                    checker_payload["raw_response"] = (reply.content or "")[:2000]
            return reply

        dynamic_module.generate = replay_generate
        result = {
            "task_id": data["task_id"],
            "stage": data["stage"],
            "source": data["source"],
            "candidate": candidate.get("utterance", ""),
        }
        try:
            _, _, verification, _ = generate_dynamic_followup(
                model=model,
                model_args=model_args,
                stage=data["stage"],
                source_request=data["source"],
                conversation=_messages(data["history"]),
                released_facts=[data["released"]],
                config=replay_config,
                tool_call_gt=tool_gt_for_stage(
                    load_task(data["task_id"]), data["stage"]
                ),
            )
            result["accepted"] = True
            result["verification"] = verification.model_dump(mode="json")
        except Exception as error:
            result["accepted"] = False
            result["error"] = _safe_error(error)
        finally:
            dynamic_module.generate = original_generate
        if checker_payload:
            result["checker_response"] = checker_payload
        results["cases"].append(result)
        print(
            f"task={data['task_id']} stage={data['stage']} "
            f"accepted={result['accepted']}",
            flush=True,
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

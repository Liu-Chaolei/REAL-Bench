"""Replay captured source-based dynamic prompts with status and affect context.

This is an offline experiment. It does not start a simulation and does not
change the graph experiment or the benchmark data. The three inputs are the
stage-1/stage-2 prompts captured from the two real Gaia2 runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from experiment_dynamic_goal_graph import (
    CASES,
    load_request_case,
    load_task,
    tool_gt_for_stage,
)

from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.user.dynamic_followup import (
    classify_status_affect,
    generate_dynamic_followup,
)
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data/experiments/dynamic_status_affect_20261118.json"


def history_messages(history: str) -> list[SimpleNamespace]:
    """Convert captured role-prefixed history into the minimal message shape."""
    messages = []
    for row in history.splitlines():
        if ": " not in row:
            continue
        role, content = row.split(": ", 1)
        if role in {"system", "user", "assistant"} and content:
            messages.append(SimpleNamespace(role=role, content=content))
    return messages


def main() -> int:
    config_path = (
        ROOT
        / "integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json"
    )
    config = QwenAgentConfig.model_validate_json(config_path.read_text())
    model, model_args = config.user_llm_settings()
    model_args = {**model_args, "num_retries": 0, "temperature": 0.8, "seed": 626729}
    dynamic_config = DynamicFollowupConfig(enabled=True, stages=[1, 2], max_retries=1)
    output = {
        "experiment": "dynamic_status_affect",
        "model": model,
        "temperature": model_args["temperature"],
        "cases": [],
    }

    for case in CASES:
        data = load_request_case(case)
        messages = history_messages(data["history"])
        context_classification, context_reply = classify_status_affect(
            model=model,
            model_args=model_args,
            conversation=messages,
            max_context_characters=dynamic_config.max_context_characters,
        )
        status = context_classification.status
        affect = context_classification.affect
        gt = tool_gt_for_stage(load_task(data["task_id"]), data["stage"])
        candidate, reply, verification, verification_reply = generate_dynamic_followup(
            model=model,
            model_args=model_args,
            stage=data["stage"],
            source_request=data["source"],
            conversation=messages,
            released_facts=[data["released"]],
            config=dynamic_config,
            tool_call_gt=gt,
            stage_status=status,
            affect=affect,
        )
        output["cases"].append(
            {
                "task_id": data["task_id"],
                "stage": data["stage"],
                "stage_status": status,
                "affect": affect,
                "context_classification": context_classification.model_dump(
                    mode="json"
                ),
                "source": data["source"],
                "previous_dynamic_utterance": data["original_utterance"],
                "new_dynamic_utterance": candidate.utterance,
                "covered_requirements": candidate.covered_requirements,
                "verification": verification.model_dump(mode="json"),
                "generation_time_seconds": reply.generation_time_seconds,
                "verification_time_seconds": verification_reply.generation_time_seconds,
                "context_generation_time_seconds": context_reply.generation_time_seconds,
                "context_cost": context_reply.cost,
            }
        )
        print(
            f"completed task={data['task_id']} stage={data['stage']} "
            f"status={status} affect={affect} valid={verification.valid}",
            flush=True,
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

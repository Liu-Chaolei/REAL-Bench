"""Evaluator-only contracts derived from ARE's resolved reference event filter."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from enum import Enum
from pathlib import Path

VERSION = "gaia2-qwen-audio/5"
SCHEMA = "gaia2-task-contract/1"


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def stable_config(value):
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: stable_config(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name not in {"engine", "tracer"}
        }
    if isinstance(value, dict):
        return {str(stable_config(k)): stable_config(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [stable_config(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        return f"{value.__module__}.{value.__qualname__}"
    raise TypeError(f"Unsupported judge configuration value: {type(value).__name__}")


def derive_contract(scenario, source_text, *, protocol=VERSION):
    if __package__:
        from .observation import eligibility
    else:
        from observation import eligibility

    support = eligibility(json.loads(source_text))
    if not support["supported"]:
        raise ValueError(f"unsupported_protocol: {support['reasons']}")
    judge = scenario.judge
    required = []
    for event in scenario.oracle_run_event_log:
        if not judge.event_filter(event) or event.event_type.value != "AGENT":
            continue
        required.append(
            {
                "event_id": event.event_id,
                "tool": event.tool_name,
                "kind": "answer"
                if event.tool_name == "AgentUserInterface__send_message_to_user"
                else "action",
            }
        )
    answers = sum(e["kind"] == "answer" for e in required)
    actions = len(required) - answers
    if not required or answers > 1:
        raise ValueError("Unsupported or empty official task contract")
    return {
        "schema": SCHEMA,
        "protocol": protocol,
        "kind": "mixed"
        if answers and actions
        else "answer_only"
        if answers
        else "action_only",
        "requires_answer": bool(answers),
        "required_action_count": actions,
        "required_answer_count": answers,
        "required_events": required,
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "judge_policy_sha256": fingerprint(
            {
                "class": type(judge).__name__,
                "filter": type(judge.event_filter).__name__,
                "config": stable_config(judge.config),
            }
        ),
        "are_version": "1.2.0",
    }


def load_contract(source, *, protocol=VERSION):
    from are.simulation.benchmark.scenario_loader import load_scenario
    from are.simulation.scenarios.scenario_imported_from_json.utils import (
        preprocess_scenario,
    )
    from are.simulation.validation.configs import GraphPerEventJudgeConfig

    def no_inference(*args, **kwargs):
        raise RuntimeError("Contract derivation must not invoke a judge model")

    text = source.read_text()
    if __package__:
        from .observation import eligibility
    else:
        from observation import eligibility
    support = eligibility(json.loads(text))
    if not support["supported"]:
        return {
            "schema": SCHEMA,
            "protocol": protocol,
            "kind": "unsupported",
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            **support,
        }
    scenario, _ = load_scenario(text, str(source), load_completed_events=False)
    if scenario is None:
        raise ValueError("Official scenario could not be loaded")
    preprocess_scenario(
        scenario,
        judge_config=GraphPerEventJudgeConfig(engine=no_inference),
        offline_validation=True,
    )
    return derive_contract(scenario, text, protocol=protocol)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--protocol",
        choices=[
            VERSION,
            "gaia2-qwen-audio/6",
            "gaia2-qwen-audio/7",
            "gaia2-qwen-audio/8",
        ],
        default=VERSION,
    )
    args = parser.parse_args()
    count = 0
    for source in sorted(args.dataset.glob("*/environment/scenario.json")):
        contract = load_contract(source, protocol=args.protocol)
        target = source.with_name("task-contract.json")
        if args.verify:
            if json.loads(target.read_text()) != contract:
                raise ValueError("Frozen official contract differs")
        else:
            if target.exists():
                raise ValueError("Preserve existing contract")
            target.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n")
        count += 1
    if not count:
        raise ValueError("No packaged scenarios")
    print(json.dumps({"contracts": count, "judge_model_calls": 0}))


if __name__ == "__main__":
    main()

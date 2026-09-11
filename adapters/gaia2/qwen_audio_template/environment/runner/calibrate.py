"""30 known-label semantic controls, 3 fresh judge calls each; official prompts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from are.simulation.validation.prompts import (
    CONTENT_CHECKER_SYSTEM_PROMPT_TEMPLATE,
    CONTENT_CHECKER_USER_PROMPT_TEMPLATE,
)
from are.simulation.validation.utils.llm_utils import build_llm_checkers
from jinja2 import Template
from responses_judge import ResponsesJudge


def cases():
    # Fifteen different entities and facts, each with a correct paraphrase and
    # an explicitly contradictory answer. Never used as benchmark user input.
    facts = [
        ("total", "42", "forty-two", "43"),
        ("meeting time", "09:00", "9 AM", "10 AM"),
        ("city", "Paris", "Paris, France", "Berlin"),
        ("file count", "100", "one hundred", "10"),
        ("age", "31", "thirty-one years old", "41"),
        ("price", "$25", "25 dollars", "$250"),
        ("date", "2026-09-09", "September 9, 2026", "September 10, 2026"),
        ("recipient", "Alice", "Alice", "Bob"),
        ("duration", "5 minutes", "300 seconds", "5 hours"),
        ("status", "cancelled", "canceled", "confirmed"),
        ("quantity", "12", "a dozen", "13"),
        ("temperature", "20 Celsius", "20 degrees Celsius", "30 Celsius"),
        ("distance", "2 km", "2000 metres", "20 km"),
        ("filename", "report.pdf", "report.pdf", "report.docx"),
        ("floor area", "2700 sqft", "2700 square feet", "270 sqft"),
    ]
    for index, (field, expected, correct, wrong) in enumerate(facts):
        for label, actual in [(True, correct), (False, wrong)]:
            yield {
                "id": f"semantic-{index:02d}-{'positive' if label else 'negative'}",
                "label": label,
                "checker_args": {
                    "agent_action_call": f"The {field} is {actual}.",
                    "oracle_action_call": f"The {field} is {expected}.",
                    "task": f"Tell me the {field}.",
                    "today_date": "2026-09-09",
                },
                "messages": [
                    {
                        "role": "system",
                        "content": CONTENT_CHECKER_SYSTEM_PROMPT_TEMPLATE,
                    },
                    {
                        "role": "user",
                        "content": Template(
                            CONTENT_CHECKER_USER_PROMPT_TEMPLATE
                        ).render(
                            agent_action_call=json.dumps(
                                {"content": f"The {field} is {actual}."}
                            ),
                            oracle_action_call=json.dumps(
                                {"content": f"The {field} is {expected}."}
                            ),
                            tool_name="AgentUserInterface__send_message_to_user",
                            task=f"Tell me the {field}.",
                            today_date="2026-09-09",
                            user_address="",
                        ),
                    },
                ],
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve calibration evidence; use a new output directory")
    args.output.mkdir(parents=True)
    judge = ResponsesJudge(
        os.environ.get("GAIA2_JUDGE_MODEL", "gpt-6-astra"),
        os.environ.get("GAIA2_JUDGE_ENDPOINT", ""),
        os.environ.get("GAIA2_JUDGE_API_KEY", ""),
        os.environ.get("GAIA2_JUDGE_REASONING_EFFORT", "low"),
        record_path=args.output / "calls.jsonl",
        replay_path=args.replay,
    )
    observations = []
    checkers = build_llm_checkers(judge)
    try:
        for case in cases():
            for repeat in range(3):
                checker_args = case["checker_args"]
                sane = checkers["sanity_checker"](
                    {
                        "agent_action_call": checker_args["agent_action_call"],
                        "task": checker_args["task"],
                    }
                )
                matches = checkers["user_message_checker"](checker_args)
                actual = None if sane is None or matches is None else sane and matches
                answer = {"sanity": sane, "user_message": matches}
                row = {
                    **case,
                    "repeat": repeat,
                    "actual": actual,
                    "answer": answer,
                    "correct": actual == case["label"],
                }
                observations.append(row)
                with (args.output / "observations.jsonl").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")
    finally:
        judge.close()
    by_label = {
        str(label): sum(r["correct"] for r in observations if r["label"] == label) / 45
        for label in [True, False]
    }
    report = {
        "positive": 15,
        "negative": 15,
        "repeats": 3,
        "agreement": sum(r["correct"] for r in observations) / 90,
        "by_label": by_label,
        "systematic_bias": any(v < 0.95 for v in by_label.values()),
    }
    (args.output / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    if report["agreement"] < 0.95 or report["systematic_bias"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

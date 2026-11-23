"""Replay saved and synthetic calls through the configured live intent judges.

This calls grading models only; it never starts an Agent or executes business tools.
Credentials are loaded from a private dotenv file and are never serialized.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from tau2.data_model.diagnostics import BackendInput, DiagnosticEvidence, UserInput
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.call import evaluate_call
from tau2.diagnostics.intent.io import digest, write_json
from tau2.diagnostics.model import RequestBudget
from tau2.diagnostics.suite import load_evidence
from tau2.utils.env_config import read_config_environment, resolve_env_reference

REQUEST = "Email Wang at wang@example.com that the meeting is at 15:00."


def scenarios():
    """Freeze positive and negative semantic examples with actual message identities."""

    def call(name, user, messages, expected_extra, expected_goals=None):
        inputs = [
            BackendInput(
                id=f"b{i}",
                content=content,
                time=i + 2,
                accepted=True,
                role="prompt" if i == 0 or independent else "steer",
                task_id=f"task-{i}" if independent else "task-0",
                session_id=f"session-{i}" if independent else "session-0",
            )
            for i, (content, independent) in enumerate(messages)
        ]
        return (
            name,
            DiagnosticEvidence(
                simulation_id=name,
                task_id=name,
                domain="gaia2",
                backend_status="complete",
                user_inputs=[UserInput(id="u", text=user, started=0, ended=1)],
                backend_inputs=inputs,
            ),
            expected_extra,
            expected_goals,
        )

    yield call("one-request-three-delegations", REQUEST, [(REQUEST, True)] * 3, 2, 3)
    yield call(
        "three-explicitly-requested",
        REQUEST + " Send three separate copies of this email.",
        [(REQUEST, True)] * 3,
        0,
        3,
    )
    yield call(
        "repeated-user-emphasis", " ".join([REQUEST] * 3), [(REQUEST, True)], 0, 1
    )
    yield call(
        "supplementary-messages",
        REQUEST,
        [
            (
                "Prepare one email to Wang at wang@example.com; I will supply its content next.",
                False,
            ),
            ("For the same email, the body is: the meeting is at 15:00.", False),
            (
                "Send that one email now; this completes the earlier request, no additional email.",
                False,
            ),
        ],
        0,
        1,
    )
    yield call(
        "final-asr-objective-restatement",
        REQUEST,
        [({"final_asr": REQUEST, "objective": REQUEST}, True)],
        0,
        1,
    )
    yield call(
        "different-recipients",
        REQUEST + " Also email Li at li@example.com the same information.",
        [
            (REQUEST, True),
            ("Email Li at li@example.com that the meeting is at 15:00.", True),
        ],
        0,
        2,
    )
    yield call(
        "different-times",
        "Email Wang at wang@example.com the meeting notice at 09:00 and again at 17:00.",
        [
            ("Email Wang at wang@example.com the meeting notice at 09:00.", True),
            ("Email Wang at wang@example.com the meeting notice at 17:00.", True),
        ],
        0,
        2,
    )
    yield call(
        "same-task-correction",
        REQUEST
        + " Correction: the meeting is at 16:00, send only the corrected notice.",
        [
            (REQUEST, False),
            (
                "Amend the same task: the meeting is at 16:00. Send only the corrected notice, not an additional email.",
                False,
            ),
        ],
        0,
        1,
    )
    again = call("later-stage-again", REQUEST, [(REQUEST, True), (REQUEST, True)], 0, 2)
    again[1].user_inputs[0].stage = 0
    again[1].user_inputs.append(
        UserInput(
            id="u1",
            text="Now send that same email once again.",
            started=2.5,
            ended=2.9,
            stage=1,
        )
    )
    yield again


async def run(args):
    """Persist reproducible inputs, raw judgments and independent acceptance results."""
    config = MetricsConfig.load(args.config)
    values = read_config_environment(args.env_file) if not config.api_config_dir else {}
    for role in ("gt", "prediction", "evaluation"):
        name = config.profile(role).api_key_env
        if name:
            os.environ[name] = resolve_env_reference(values, name)
    cases = list(scenarios())
    if args.saved_evidence:
        saved = load_evidence(args.saved_evidence)
        cases.append(("saved-whole-call", saved, None, None))
        if args.duplicate_saved:
            copied = saved.model_copy(deep=True)
            original = next(
                row
                for row in reversed(copied.backend_inputs)
                if row.role == "prompt" and not row.internal and row.accepted
            )
            duplicate = original.model_copy(
                update={
                    "id": original.id + "-validation-duplicate",
                    "session_id": "validation-independent-session",
                    "task_id": "validation-independent-task",
                    "logical_id": None,
                    "time": max(row.time for row in copied.backend_inputs) + 1,
                }
            )
            copied.backend_inputs.append(duplicate)
            copied.simulation_id += "-duplicate-validation"
            cases.append(("saved-whole-call-duplicate", copied, None, None))
    if args.case:
        cases = [case for case in cases if case[0] in args.case]
        if not cases:
            raise ValueError("No matching acceptance cases")
    report = {"version": "tau-intent-repetition-validation/1", "cases": []}
    for name, evidence, expected_extra, expected_goals in cases:
        directory = args.output / name
        source_path = directory / "evidence.json"
        if source_path.exists() and digest(evidence.model_dump(mode="json")) != digest(
            json.loads(source_path.read_text())
        ):
            raise ValueError("Validation output already contains different evidence")
        write_json(source_path, evidence.model_dump(mode="json"))
        print(f"Scoring {name}", flush=True)
        scores = await evaluate_call(
            evidence, config, directory / "intent", RequestBudget(config.max_requests)
        )
        repetition = scores.get("repetition", {})
        passed = scores["status"] == "complete"
        if expected_extra is not None:
            passed &= repetition.get("extra_goal_count") == expected_extra
        if expected_goals is not None:
            passed &= repetition.get("total_goal_count") == expected_goals
        if expected_extra:
            passed &= (
                scores.get("precision") is not None
                and scores["precision"] <= scores["content_scores"]["precision"]
                and scores["precision"] < 1
            )
        if name == "saved-whole-call-duplicate":
            passed &= repetition.get("extra_goal_count", 0) >= 1
        row = {
            "name": name,
            "passed": passed,
            "status": scores["status"],
            "expected_extra": expected_extra,
            "expected_goals": expected_goals,
            "extra_goals": repetition.get("extra_goal_count"),
            "goals": repetition.get("total_goal_count"),
            "content_precision": scores.get("content_scores", {}).get("precision"),
            "precision": scores.get("precision"),
            "recall": scores.get("recall"),
            "f1": scores.get("f1"),
            "artifacts": str(directory),
        }
        report["cases"].append(row)
        report["passed"] = all(case["passed"] for case in report["cases"])
        write_json(args.output / "report.json", report)
        print(
            f"{name}: status={row['status']}, extras={row['extra_goals']}, passed={passed}",
            flush=True,
        )
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("examples/real-metrics.deepseek.yaml")
    )
    parser.add_argument(
        "--env-file", default=str(Path.home() / ".config/qwaudio/config.env")
    )
    parser.add_argument("--saved-evidence", type=Path)
    parser.add_argument(
        "--duplicate-saved",
        action="store_true",
        help="Also grade an isolated copy with the final prompt independently repeated",
    )
    parser.add_argument(
        "--case", action="append", help="Run only the named acceptance case; repeatable"
    )
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)

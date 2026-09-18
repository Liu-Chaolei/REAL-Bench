"""Offline projection and official score replay, using only recorded judge replies."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from are.simulation.data_handler.importer import JsonScenarioImporter
from are.simulation.scenario_runner import ScenarioRunner
from are.simulation.scenarios.config import ScenarioRunnerConfig
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from boundary import resolve_votes
from evidence import evaluate_evidence, validate_grade
from observation import VERSION as WINDOW_VERSION
from observation import project_audio
from responses_judge import ResponsesJudge
from task_contract import VERSION as TASK_VERSION
from task_contract import derive_contract


def verify_projection(events, protocol_rows):
    official = [
        e
        for e in events
        if e.get("event_type") == "AGENT"
        and e.get("action", {}).get("app") == "AgentUserInterface"
        and e["action"].get("function") == "send_message_to_user"
    ]
    official.sort(key=lambda e: e["event_time"])
    projected = [
        r
        for r in protocol_rows
        if r["event"] == "delivery.committed" and r["kind"] != "progress"
    ]
    if len(official) != len(projected):
        raise ValueError("Formal delivery count differs from official AUI events")
    assessments = {
        r["delivery_id"]: r for r in protocol_rows if r["event"] == "delivery.assessed"
    }
    version3 = any(r.get("protocol") == "gaia2-qwen-audio/3" for r in protocol_rows)
    if version3:
        for row in protocol_rows:
            if row["event"] != "delivery.committed":
                continue
            assessment = assessments.get(row["delivery_id"])
            if not assessment or assessment.get("assessed_kind") != row["kind"]:
                raise ValueError(
                    "Missing or inconsistent independent boundary evidence"
                )
            evidence = assessment["evidence"]
            votes = evidence.get("votes", [])
            if len(votes) != 2 or resolve_votes(votes) != row["kind"]:
                raise ValueError(
                    "Boundary votes do not reproduce committed classification"
                )
    identities = set()
    for turn, (event, row) in enumerate(zip(official, projected, strict=True), 1):
        content = next(
            arg["value"] for arg in event["action"]["args"] if arg["name"] == "content"
        )
        if content != row["transcript"] or row["are_turn"] != turn:
            raise ValueError("Formal delivery text or turn differs from official event")
        if row["delivery_id"] in identities:
            raise ValueError("Duplicate formal delivery")
        identities.add(row["delivery_id"])
    return len(projected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    integrity_path = args.results / "integrity.json"
    if integrity_path.exists():
        integrity = json.loads(integrity_path.read_text())
        for filename, expected in integrity["files"].items():
            path = (args.results / filename).resolve()
            if (
                not path.is_relative_to(args.results.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise ValueError("Artifact integrity check failed")
    result = json.loads((args.results / "result.json").read_text())
    if result.get("protocol") in {
        "gaia2-qwen-audio/6",
        "gaia2-qwen-audio/7",
        "gaia2-qwen-audio/8",
    }:
        from native_recovery import verify_recorded_recovery

        verify_recorded_recovery(args.results, result)
    v7 = result.get("protocol") in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}
    if result["status"] != "completed" and not (
        v7 and result["status"] == "completed_unscored"
    ):
        raise ValueError("Only completed trials have an official score to replay")
    traces = list((args.results / "official/hf").glob("*.json"))
    if len(traces) != 1:
        raise ValueError("Expected exactly one official trace")
    trace_json = traces[0].read_text()
    trace = json.loads(trace_json)
    protocol_rows = [
        json.loads(line)
        for line in (args.results / "protocol.jsonl").read_text().splitlines()
    ]
    if result.get("protocol") in {
        TASK_VERSION,
        "gaia2-qwen-audio/6",
        "gaia2-qwen-audio/7",
        "gaia2-qwen-audio/8",
    }:
        validate_grade(result)
        contract = json.loads((args.results / "task-contract.json").read_text())
        projection = evaluate_evidence(
            args.results,
            contract,
            end_reason=result["timing"]["observation_end_reason"],
        )
        saved = json.loads((args.results / "projection.json").read_text())
        if not v7 and any(saved.get(k) != v for k, v in projection.items()):
            raise ValueError("V5 task evidence differs")
        official = [
            e
            for e in trace["completed_events"]
            if e.get("event_type") == "AGENT"
            and e.get("action", {}).get("app") == "AgentUserInterface"
            and e["action"].get("function") == "send_message_to_user"
        ]
        submission_text = projection["task_submission_text"]
        if len(official) != bool(submission_text):
            raise ValueError("Unexpected task submission")
        if (
            official
            and next(
                a["value"]
                for a in official[0]["action"]["args"]
                if a["name"] == "content"
            )
            != submission_text
        ):
            raise ValueError("Task submission text differs")
        count = len(official)
    elif result.get("protocol") == WINDOW_VERSION:
        projection = project_audio(args.results)
        saved = json.loads((args.results / "projection.json").read_text())
        if not projection["audible_transcript_complete"] or any(
            saved.get(k) != v for k, v in projection.items()
        ):
            raise ValueError("Audible projection is incomplete or changed")
        official = [
            e
            for e in trace["completed_events"]
            if e.get("event_type") == "AGENT"
            and e.get("action", {}).get("app") == "AgentUserInterface"
            and e["action"].get("function") == "send_message_to_user"
        ]
        if len(official) != bool(projection["text"]):
            raise ValueError("V4 must submit the entire observation exactly once")
        if official:
            content = next(
                a["value"]
                for a in official[0]["action"]["args"]
                if a["name"] == "content"
            )
            if content != projection["text"]:
                raise ValueError("Official text differs from actually played speech")
        if result.get("boundary_responses_calls") != 0:
            raise ValueError("V4 must not call a boundary model")
        count = len(official)
    else:
        count = verify_projection(trace["completed_events"], protocol_rows)
    if result.get("protocol") == "gaia2-qwen-audio/3":
        boundary_path = args.results / "boundary-calls.jsonl"
        calls = (
            [json.loads(line) for line in boundary_path.read_text().splitlines()]
            if boundary_path.exists()
            else []
        )
        for assessment in (
            r for r in protocol_rows if r["event"] == "delivery.assessed"
        ):
            evidence = assessment["evidence"]
            matched = []
            for call in calls:
                body = call["payload"]["input"][-1]["content"]
                fingerprint = hashlib.sha256(
                    (evidence["version"] + body).encode()
                ).hexdigest()
                if fingerprint == evidence["input_sha256"]:
                    matched.append(call)
            if (
                len(matched) < 2
                or [json.loads(call["text"]) for call in matched[:2]]
                != evidence["votes"]
            ):
                raise ValueError(
                    "Independent boundary decisions do not match recorded assessor requests"
                )
            spoken = json.loads(matched[0]["payload"]["input"][-1]["content"])["spoken"]
            committed = next(
                (
                    r
                    for r in protocol_rows
                    if r["event"] == "delivery.committed"
                    and r["delivery_id"] == assessment["delivery_id"]
                ),
                None,
            )
            if committed and committed["transcript"] != spoken:
                raise ValueError(
                    "Boundary assessment was attached to another spoken response"
                )

    judge_path = args.results / "judge-calls.jsonl"
    # Even zero-call traces must prohibit fresh model requests.
    if not judge_path.exists():
        empty = args.output.with_suffix(".empty-judge.jsonl")
        empty.write_text("")
        judge_path = empty
    judge = ResponsesJudge(
        result["models"]["GAIA2_JUDGE_MODEL"],
        "",
        "",
        effort=result["models"]["GAIA2_JUDGE_REASONING_EFFORT"],
        replay_path=judge_path,
    )
    try:
        scenario, events, _ = JsonScenarioImporter().import_from_json_to_benchmark(
            trace_json, load_completed_events=True
        )
        preprocess_scenario(
            scenario,
            judge_config=GraphPerEventJudgeConfig(engine=judge),
            offline_validation=True,
        )
        if result.get("protocol") in {
            TASK_VERSION,
            "gaia2-qwen-audio/6",
            "gaia2-qwen-audio/7",
            "gaia2-qwen-audio/8",
        }:
            source_path = Path(__import__("os").environ["GAIA2_REPLAY_SOURCE"])
            if (
                derive_contract(
                    scenario, source_path.read_text(), protocol=result["protocol"]
                )
                != contract
            ):
                raise ValueError("Replayed official contract differs")
        if v7:
            from decision_audit import verify_decision

            evidence = verify_decision(
                args.results, result, scenario, events, source_path.read_text()
            )
            if evidence["score_basis"] != "official_full_judge":
                if judge.replay or result.get("judge_responses_calls", 0):
                    raise ValueError(
                        "Deterministic decisions cannot contain judge calls"
                    )
                args.output.write_text(
                    json.dumps(
                        {
                            "passed": True,
                            "success": result["success"],
                            "score_basis": evidence["score_basis"],
                            "verification_kind": "official_necessary_condition"
                            if evidence["score_basis"]
                            == "official_action_count_failure"
                            else "unresolved_evidence",
                            "judge_calls_replayed": 0,
                            "fresh_model_calls": 0,
                        },
                        indent=2,
                    )
                    + "\n"
                )
                return
        replayed = ScenarioRunner().run(
            ScenarioRunnerConfig(judge_only=True), scenario, completed_events=events
        )
        if replayed.exception:
            raise replayed.exception
        if replayed.success != result["success"]:
            raise ValueError("Official replay score differs from recorded score")
        if judge.calls != len(judge.replay):
            raise ValueError("Replay did not consume exactly the recorded judge calls")
        args.output.write_text(
            json.dumps(
                {
                    "passed": True,
                    "formal_deliveries": count,
                    "success": replayed.success,
                    "judge_calls_replayed": judge.calls,
                    "fresh_model_calls": 0,
                },
                indent=2,
            )
        )
    finally:
        judge.close()


if __name__ == "__main__":
    main()

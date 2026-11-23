"""Audit a real persistent-task artifact without rerunning the evaluated Agent."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.persistent_tasks.backend_runtime import replay
from tau2.persistent_tasks.grading import grade
from tau2.persistent_tasks.scores import completion_score


def validate(evidence_path, simulation_path, *, regrade=False):
    """Distinguish complete measurements, successful behavior and unasked cases."""
    summary = json.loads(evidence_path.read_text())
    replayed = replay(summary)
    original = summary.get("grading", {})
    grading = original
    if regrade:
        if not original.get("judge"):
            raise ValueError("Regrading requires the original explicit judge profile")
        regrade_summary = dict(summary)
        regrade_summary.pop("grading", None)
        grading = grade(
            regrade_summary, CommunicateScoring.model_validate(original["judge"])
        )
        for field in ("retained_before_cancel",):
            if field in original:
                grading[field] = original[field]
    simulation = json.loads(simulation_path.read_text())
    recorded = (simulation.get("info") or {}).get("persistent_tasks", {})
    if recorded.get("evidence_sha256") != summary["evidence_sha256"]:
        raise ValueError("Evidence and simulation belong to different runs")
    measured = (
        not replayed["issues"]
        and grading.get("status") == "completed"
        and simulation.get("evaluation_status") == "completed"
        and grading.get("evaluation_log", {})
        .get("coverage", grading.get("coverage", {}))
        .get("delivered")
        == len(summary["config"]["requests"])
    )
    passed = measured and completion_score(grading) == 1.0
    reasons = []
    evidence_diagnostic_reasons = [item["reason"] for item in replayed["issues"]]
    if not measured:
        if grading.get("status") != "completed":
            reasons.append(grading.get("status", "ungraded"))
        if grading.get("evaluation_log", {}).get(
            "coverage", grading.get("coverage", {})
        ).get("delivered") != len(summary["config"]["requests"]):
            reasons.append("required_request_not_delivered")
        if simulation.get("evaluation_status") != "completed":
            reasons.append("evaluation_failed")
        if grading.get("status") != "not_reached":
            reasons.extend(evidence_diagnostic_reasons)
    if measured and not passed:
        reasons.append("measured_nonpassing")
    reasons = sorted(set(reasons))
    states = sorted({transition["current"] for transition in summary["transitions"]})
    attribution_counts = {}
    for operation in summary.get("operations", []):
        key = operation.get("attribution", "unbound")
        attribution_counts[key] = attribution_counts.get(key, 0) + 1
    reply_windows = summary.get("reply_windows", {})
    transcript_counts = {}
    for window in reply_windows.values():
        key = window.get("audio_transcript_status", "unknown")
        transcript_counts[key] = transcript_counts.get(key, 0) + 1
    diagnostics = summary.get("audio_diagnostics", [])
    diagnostic_counts = {}
    for diagnostic in diagnostics:
        key = diagnostic.get("reason", "unknown")
        diagnostic_counts[key] = diagnostic_counts.get(key, 0) + 1
    planned_failed_query = any(
        request.get("operation") == "query" and request.get("state") == "failed"
        for request in summary.get("config", {}).get("requests", [])
    )
    observed_failed = any(
        transition.get("current") == "failed"
        for transition in summary.get("transitions", [])
    )
    return {
        "version": "persistent-task-acceptance-v1"
        if summary.get("evidence_version")
        else "qwen-persistent-acceptance-v2",
        "task_id": simulation.get("task_id"),
        "scenario_id": summary["config"]["scenario_id"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed"
        if passed
        else ("measured_nonpassing" if measured else "unverified"),
        "measurement_verified": bool(measured),
        "expected_behavior_passed": bool(passed),
        "evidence": str(evidence_path),
        "simulation": str(simulation_path),
        "evidence_sha256": summary["evidence_sha256"],
        "config_sha256": summary["config_sha256"],
        "replay": "matched",
        "observed_states": states,
        "reasons": reasons,
        "evidence_diagnostic_reasons": sorted(set(evidence_diagnostic_reasons)),
        "original_grading_version": original.get("version"),
        "grading": grading,
        "original_gaia2_reward": (simulation.get("reward_info") or {}).get("reward"),
        "reply_evidence_version": summary.get("reply_evidence_version", "legacy"),
        "reply_source": summary.get("config", {}).get(
            "reply_evidence_mode", "generated_text"
        ),
        "operation_attribution_counts": attribution_counts,
        "audio_transcript_status_counts": transcript_counts,
        "audio_diagnostic_counts": diagnostic_counts,
        "boundary_audio_diagnostic_count": sum(
            count
            for reason, count in diagnostic_counts.items()
            if reason.startswith("boundary_overlap")
        ),
        "invalid_audio_alignment_count": sum(
            count
            for reason, count in diagnostic_counts.items()
            if "audio_alignment" in reason
        ),
        "planned_failed_query": planned_failed_query,
        "unsupported_failed_query": planned_failed_query and not observed_failed,
        "evidence_issue_count": len(replayed["issues"]),
        "agent_image_id": (simulation.get("agent_system_metadata") or {}).get(
            "image_id"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--simulation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--regrade",
        action="store_true",
        help="Recompute grading; reuse identical frozen reply verdicts when available",
    )
    args = parser.parse_args()
    report = validate(args.evidence, args.simulation, regrade=args.regrade)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "scenario_id",
                    "status",
                    "measurement_verified",
                    "expected_behavior_passed",
                    "replay",
                    "original_gaia2_reward",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

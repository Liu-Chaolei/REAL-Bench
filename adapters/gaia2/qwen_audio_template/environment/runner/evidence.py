"""V5: distinguish normal barge-in evidence gaps from damaged recordings."""

from __future__ import annotations

import json
from pathlib import Path

if __package__:
    from .observation import project_audio
    from .task_contract import VERSION, fingerprint
else:
    from observation import project_audio
    from task_contract import VERSION, fingerprint


def normal_interruption(metadata, events):
    identity = metadata.get("response_id")
    if (
        metadata.get("cancelled") is not True
        or metadata.get("played_completely") is not False
        or not metadata.get("played_samples")
    ):
        return False
    received = metadata.get("received_samples")
    if not isinstance(received, int) or received < metadata["played_samples"]:
        return False
    starts = [
        e["wall_time"]
        for e in events
        if e.get("type") == "playback.started" and e.get("responseId") == identity
    ]
    cancels = [
        e["wall_time"]
        for e in events
        if e.get("type") == "playback.cancelled" and e.get("responseId") == identity
    ]
    clears = [
        e["wall_time"]
        for e in events
        if e.get("type") == "playback.clear" and e.get("reason") == "user_interruption"
    ]
    return any(
        start <= clear <= cancel and cancel - clear <= 2
        for start in starts
        for clear in clears
        for cancel in cancels
    )


def evaluate_evidence(root: Path, contract, *, end_reason=None):
    projected = project_audio(root)
    events_path = root / "gateway.jsonl"
    events = (
        [json.loads(line) for line in events_path.read_text().splitlines()]
        if events_path.exists()
        else []
    )
    v8 = contract.get("protocol") == "gaia2-qwen-audio/8"
    interruption_report = None
    if v8:
        if __package__:
            from .interruption_evidence import build_interruption_evidence
        else:
            from interruption_evidence import build_interruption_evidence
        interruption_report = build_interruption_evidence(root)
    associated = (
        {r["response_id"]: r for r in interruption_report["responses"]}
        if interruption_report
        else {}
    )
    interrupted, damaged, unverified = [], [], []
    for gap in projected["gaps"]:
        path = root / gap["file"]
        metadata = (
            json.loads(path.read_text())
            if path.suffix == ".json" and path.exists()
            else {}
        )
        association = associated.get(metadata.get("response_id"), {})
        normal = (
            association.get("decision") == "normal_interruption"
            if v8
            else normal_interruption(metadata, events)
        )
        if gap["reason"] == "audible_transcript_incomplete" and normal:
            interrupted.append(
                {
                    **gap,
                    "response_id": metadata["response_id"],
                    "played_samples": metadata["played_samples"],
                }
            )
        elif (
            end_reason == "total_timeout"
            and gap["reason"] == "audible_transcript_incomplete"
            and metadata.get("played_completely") is False
            and metadata.get("played_samples", 0) > 0
            and (not v8 or metadata.get("cancelled") is False)
        ):
            interrupted.append(
                {
                    **gap,
                    "reason": "budget_cutoff",
                    "response_id": metadata.get("response_id"),
                }
            )
        elif (
            v8
            and gap["reason"] == "audible_transcript_incomplete"
            and association.get("reason") != "invalid_playback_metadata"
        ):
            unverified.append(
                {
                    **gap,
                    "response_id": metadata.get("response_id"),
                    "reason": association.get("reason", "unattributed_cancellation"),
                }
            )
        else:
            damaged.append(gap)
    speech = (
        "missing_or_corrupt"
        if damaged
        else "unverified_interruption"
        if unverified
        else "partial_interruption"
        if interrupted
        else "complete"
    )
    score = (
        "invalid_execution"
        if damaged or unverified
        else "scored"
        if end_reason == "total_timeout"
        and contract.get("protocol") not in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}
        else "unresolved_speech"
        if interrupted and contract["requires_answer"]
        else "scored"
    )
    text = (
        projected["text"]
        if score == "scored" and contract["requires_answer"] and speech == "complete"
        else None
    )
    result = {
        "protocol": contract.get("protocol", VERSION),
        "contract_sha256": fingerprint(contract),
        "task_contract_kind": contract["kind"],
        "task_score_status": score,
        "speech_evidence_status": speech,
        "audible_transcript_complete": speech == "complete",
        "complete_segments": projected["segments"],
        "interrupted_segments": interrupted,
        "hard_errors": damaged,
        "task_submission_text": text,
        "task_submission_sha256": fingerprint(text) if text else None,
        "observation_end_reason": end_reason,
        "semantic_equivalence_verified": False,
    }
    if v8:
        result["interruption_evidence_sha256"] = fingerprint(interruption_report)
        result["unverified_interruptions"] = unverified
    if contract.get("protocol") in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}:
        result["score_basis"] = (
            "official_full_judge"
            if score == "scored"
            else "unresolved_speech"
            if score == "unresolved_speech"
            else None
        )
    return result


def is_unresolved(result):
    return (
        result.get("protocol")
        in {VERSION, "gaia2-qwen-audio/6", "gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}
        and result.get("status") == "completed_unscored"
        and result.get("task_score_status") == "unresolved_speech"
        and result.get("speech_evidence_status") == "partial_interruption"
        and result.get("success") is None
        and result.get("measurement_valid") is None
    )


def validate_grade(result):
    if result.get("protocol") not in {
        VERSION,
        "gaia2-qwen-audio/6",
        "gaia2-qwen-audio/7",
        "gaia2-qwen-audio/8",
    }:
        raise ValueError("Expected v5 evidence")
    if result.get("boundary_responses_calls") != 0:
        raise ValueError("V5 cannot use a boundary model")
    if result.get("protocol") in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}:
        basis = result.get("score_basis")
        if basis not in {
            "official_full_judge",
            "official_action_count_failure",
            "unresolved_speech",
        }:
            raise ValueError("Missing V7 score basis")
        if basis == "unresolved_speech" and not is_unresolved(result):
            raise ValueError("Unknown speech cannot produce a score")
        if basis == "official_action_count_failure" and (
            result.get("success") is not False
            or result.get("status") != "completed"
            or result.get("speech_evidence_status") != "partial_interruption"
            or result.get("judge_responses_calls", 0) != 0
            or not result.get("decision_proof_sha256")
        ):
            raise ValueError("Invalid official count failure")
        if is_unresolved(result) and basis != "unresolved_speech":
            raise ValueError("Unresolved result has a scoring basis")
    if is_unresolved(result):
        if result.get("judge_responses_calls", 0) != 0:
            raise ValueError("Unresolved task must not have been judged")
        return {}
    if (
        result.get("task_score_status") != "scored"
        or result.get("measurement_valid") is not True
        or result.get("speech_evidence_status")
        not in {"complete", "partial_interruption"}
        or result.get("status")
        not in {"completed", "protocol_failure", "execution_timeout"}
        or not isinstance(result.get("success"), bool)
    ):
        raise ValueError("Invalid task scoring evidence")
    return {"reward": int(result["status"] == "completed" and result["success"])}

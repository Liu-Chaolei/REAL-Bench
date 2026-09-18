"""Independently reproduce V7 decisions and playback chronology from archives."""

from __future__ import annotations

import json

from action_decision import VERSION, action_count_proof, apply_decision
from evidence import evaluate_evidence
from playback_timeline import build_timeline


def verify_decision(root, result, scenario, events, source_text):
    if result["protocol"] not in {VERSION, "gaia2-qwen-audio/8"}:
        raise ValueError("Expected V7 evidence")
    contract = json.loads((root / "task-contract.json").read_text())
    evidence = evaluate_evidence(
        root, contract, end_reason=result["timing"]["observation_end_reason"]
    )
    v8 = result["protocol"] == "gaia2-qwen-audio/8"
    if v8:
        from interruption_evidence import verify_interruption_evidence

        verify_interruption_evidence(root)
        if (
            result.get("interruption_evidence_sha256")
            != evidence["interruption_evidence_sha256"]
        ):
            raise ValueError("Interruption digest differs from result")
    proof_path = root / "decision-proof.json"
    if evidence["task_score_status"] == "unresolved_speech":
        proof = action_count_proof(scenario, events, contract, source_text)
        if json.loads(proof_path.read_text()) != proof:
            raise ValueError("Action count proof differs from official events")
        evidence = apply_decision(evidence, proof)
    elif proof_path.exists():
        raise ValueError("Unexpected action count certificate")
    saved = json.loads((root / "projection.json").read_text())
    if any(saved.get(k) != v for k, v in evidence.items()):
        raise ValueError("V7 evidence projection differs")
    for key in (
        "task_score_status",
        "speech_evidence_status",
        "score_basis",
        "decision_proof_sha256",
    ):
        if result.get(key) != evidence.get(key):
            raise ValueError("V7 result differs from recomputed decision")
    if json.loads((root / "playback-timeline.json").read_text()) != build_timeline(
        root, include_interruptions=v8
    ):
        raise ValueError("Playback chronology differs from archived events")
    return evidence

"""Read only evidence collected from the trusted ARE service."""

import json
from pathlib import Path

result = json.loads(Path("/evaluation/results/result.json").read_text())
if result.get("protocol") in {"gaia2-qwen-audio/7", "gaia2-qwen-audio/8"}:
    basis = result.get("score_basis")
    if basis not in {
        "official_full_judge",
        "official_action_count_failure",
        "unresolved_speech",
    }:
        raise ValueError("Missing V7 score basis")
    if basis == "official_action_count_failure" and (
        result.get("success") is not False
        or result.get("status") != "completed"
        or result.get("judge_responses_calls", 0) != 0
        or not result.get("decision_proof_sha256")
    ):
        raise ValueError("Invalid count failure")
    if (basis == "unresolved_speech") != (result.get("status") == "completed_unscored"):
        raise ValueError("Inconsistent V7 scoring state")
if result.get("protocol") in {
    "gaia2-qwen-audio/5",
    "gaia2-qwen-audio/6",
    "gaia2-qwen-audio/7",
    "gaia2-qwen-audio/8",
}:
    if result.get("status") == "completed_unscored":
        if (
            result.get("success") is not None
            or result.get("measurement_valid") is not None
            or result.get("task_score_status") != "unresolved_speech"
            or result.get("speech_evidence_status") != "partial_interruption"
        ):
            raise ValueError("Invalid unscored result")
        reward = {}
    elif (
        result.get("task_score_status") == "scored"
        and result.get("measurement_valid") is True
        and isinstance(result.get("success"), bool)
        and result.get("status")
        in {"completed", "execution_timeout", "protocol_failure"}
    ):
        reward = {"reward": int(result["status"] == "completed" and result["success"])}
    else:
        raise ValueError("Invalid v5 task evidence")
    Path("/logs/verifier/reward.json").write_text(json.dumps(reward))
    Path("/logs/verifier/gaia2_qwen_audio_result.json").write_text(
        json.dumps(result, indent=2)
    )
else:
    if result.get("measurement_valid") is False:
        raise RuntimeError("Measurement is invalid; no task reward may be emitted")
    if result["status"] not in {"completed", "execution_timeout", "protocol_failure"}:
        raise RuntimeError(f"GAIA2 infrastructure error: {result}")
    if result["status"] == "completed" and not isinstance(result.get("success"), bool):
        raise ValueError("Missing official success decision")
    reward = 1 if result["status"] == "completed" and result["success"] else 0
    Path("/logs/verifier/reward.txt").write_text(str(reward))
    Path("/logs/verifier/gaia2_qwen_audio_result.json").write_text(
        json.dumps(result, indent=2)
    )

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from action_decision import BASIS, VERSION
from count_driver import CountDriver
from evidence import evaluate_evidence, validate_grade
from passive_driver import PassiveDriver

from adapters.gaia2.campaign_runner import stops_batch
from adapters.gaia2.task_metric import compute


def result():
    return {
        "protocol": VERSION,
        "status": "completed",
        "success": False,
        "measurement_valid": True,
        "task_score_status": "scored",
        "speech_evidence_status": "partial_interruption",
        "score_basis": BASIS,
        "decision_proof_sha256": "proof",
        "judge_responses_calls": 0,
        "boundary_responses_calls": 0,
    }


def test_verified_count_failure_is_zero_and_does_not_stop_batch():
    r = result()
    assert validate_grade(r) == {"reward": 0}
    assert not stops_batch(r)
    summary = compute([{"reward": 1}, validate_grade(r), {}])
    assert summary["task_pass_rate"] == 0.5
    assert summary["score_coverage"] == 2 / 3
    assert summary["unresolved_count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"success": True},
        {"measurement_valid": False},
        {"status": "infrastructure_error"},
        {"judge_responses_calls": 1},
        {"decision_proof_sha256": None},
        {"score_basis": None},
    ],
)
def test_bad_certificates_never_become_valid_scores(change):
    r = {**result(), **change}
    with pytest.raises(ValueError):
        validate_grade(r)
    assert stops_batch(r)


def test_v7_budget_cutoff_does_not_turn_unknown_speech_into_zero(tmp_path):
    meta = {
        "response_id": "r",
        "cancelled": False,
        "played_completely": False,
        "played_samples": 20,
    }
    (tmp_path / "r.json").write_text(json.dumps(meta))
    projected = {
        "gaps": [{"file": "r.json", "reason": "audible_transcript_incomplete"}],
        "text": "",
        "segments": [],
    }
    with patch("evidence.project_audio", return_value=projected):
        old = evaluate_evidence(
            tmp_path,
            {
                "protocol": "gaia2-qwen-audio/6",
                "requires_answer": True,
                "kind": "mixed",
            },
            end_reason="total_timeout",
        )
        new = evaluate_evidence(
            tmp_path,
            {"protocol": VERSION, "requires_answer": True, "kind": "mixed"},
            end_reason="total_timeout",
        )
    assert old["task_score_status"] == "scored"
    assert new["task_score_status"] == "unresolved_speech"
    assert new["score_basis"] == "unresolved_speech"


@pytest.mark.asyncio
@pytest.mark.parametrize("basis", [BASIS, "unresolved_speech", "official_full_judge"])
async def test_timeout_driver_preserves_decision_or_unknown(basis):
    driver = object.__new__(CountDriver)
    driver.task_evidence = {"score_basis": basis}
    with patch.object(PassiveDriver, "run", AsyncMock(side_effect=TimeoutError)):
        if basis == "official_full_judge":
            with pytest.raises(BaseExceptionGroup):
                await driver.run()
        else:
            await driver.run()

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from evidence import evaluate_evidence
from interruption_evidence import classify_interruptions


def meta(identity="old"):
    return {
        "response_id": identity,
        "played_samples": 100,
        "received_samples": 200,
        "cancelled": True,
        "played_completely": False,
    }


def event(kind, identity=None, **fields):
    return {
        "type": kind,
        "direction": "client"
        if kind in {"playback.started", "playback.cancelled", "playback.ended"}
        else "server",
        **({"responseId": identity} if identity else {}),
        **fields,
    }


def terminal(identity="old", status="cancelled", reason="turn_detected"):
    return event(
        "evaluation.provider_event",
        payload={
            "event_id": "done-" + identity,
            "type": "response.done",
            "response": {
                "id": identity,
                "status": status,
                "status_details": {"reason": reason},
            },
        },
    )


def base():
    return [
        event("response.started", "old", turnId="turn1"),
        event("playback.started", "old"),
        terminal(),
        event("playback.cancelled", "old", reason="provider_cancelled"),
    ]


def decision(rows, identity="old"):
    rows = [{**r, "wall_time": float(i)} for i, r in enumerate(rows)]
    return classify_interruptions([meta(identity)], rows)["responses"][0]


@pytest.mark.parametrize("position", [None, 2, 3, 4])
def test_same_response_provider_proof_independent_of_clear_order(position):
    rows = base()
    if position is not None:
        rows.insert(position, event("playback.clear", reason="user_interruption"))
    d = decision(rows)
    assert d["decision"] == "normal_interruption"
    assert d["basis"] == "provider_response"


def test_terminal_arriving_after_cancel_still_matches_same_response():
    rows = base()
    rows[-2:] = list(reversed(rows[-2:]))
    assert decision(rows)["decision"] == "normal_interruption"


@pytest.mark.parametrize(
    "status,reason",
    [
        ("failed", "turn_detected"),
        ("incomplete", "turn_detected"),
        ("cancelled", "other"),
        ("cancelled", None),
    ],
)
def test_generic_cancelled_flag_cannot_hide_provider_failure(status, reason):
    rows = base()
    rows[2] = terminal(status=status, reason=reason)
    rows.insert(2, event("playback.clear", reason="user_interruption"))
    assert decision(rows)["decision"] == "unverified_interruption"


def test_unique_active_clear_can_prove_playback_barge_in_after_generation():
    rows = base()
    rows[2] = terminal(status="completed", reason=None)
    rows.insert(3, event("playback.clear", reason="user_interruption"))
    assert decision(rows)["basis"] == "unique_active_clear"


def test_unscoped_late_clear_cannot_be_borrowed_by_new_response():
    rows = base() + [
        event("playback.started", "new"),
        event("playback.clear", reason="user_interruption"),
        event("playback.cancelled", "new"),
    ]
    report = classify_interruptions([meta(), meta("new")], rows)
    assert report["responses"][0]["decision"] == "normal_interruption"
    assert report["responses"][1]["decision"] == "unverified_interruption"
    assert report["clears"][0]["ambiguity"] == "late_clear_with_new_response"


def test_other_response_terminal_and_multiple_active_clear_are_not_proof():
    rows = base()
    rows[2] = terminal("other")
    rows.insert(2, event("playback.started", "other"))
    rows.insert(3, event("playback.clear", reason="user_interruption"))
    assert decision(rows)["decision"] == "unverified_interruption"


@pytest.mark.parametrize(
    "change",
    [
        "no_start",
        "no_cancel",
        "conflicting_terminal",
        "reused_id",
        "conflicting_turn",
        "client_forgery",
        "ended",
    ],
)
def test_incomplete_or_conflicting_lifecycle_is_rejected(change):
    rows = base()
    if change == "no_start":
        rows.pop(1)
    elif change == "no_cancel":
        rows.pop()
    elif change == "conflicting_terminal":
        rows.append(terminal(status="failed", reason="server_error"))
    elif change == "reused_id":
        rows.append(event("playback.started", "old"))
    elif change == "conflicting_turn":
        rows.append(event("response.started", "old", turnId="turn2"))
    elif change == "client_forgery":
        rows[2]["direction"] = "client"
    else:
        rows.append(event("playback.ended", "old"))
    assert decision(rows)["decision"] == "unverified_interruption"


def test_duplicate_identical_terminal_is_idempotent():
    rows = base()
    rows.insert(3, copy.deepcopy(rows[2]))
    assert decision(rows)["decision"] == "normal_interruption"


def test_unknown_reason_is_not_called_corrupt_audio(tmp_path):
    metadata = meta()
    (tmp_path / "response-0001.json").write_text(json.dumps(metadata))
    rows = base()
    rows[2] = terminal(reason="unknown")
    (tmp_path / "gateway.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    contract = {
        "protocol": "gaia2-qwen-audio/8",
        "kind": "mixed",
        "requires_answer": True,
    }
    with patch(
        "evidence.project_audio",
        return_value={
            "gaps": [
                {
                    "file": "response-0001.json",
                    "reason": "audible_transcript_incomplete",
                }
            ],
            "segments": [],
            "text": None,
        },
    ):
        result = evaluate_evidence(tmp_path, contract, end_reason="idle_window")
    assert result["speech_evidence_status"] == "unverified_interruption"
    assert result["task_score_status"] == "invalid_execution"
    assert result["hard_errors"] == []


def test_wave_damage_cannot_be_overridden_by_normal_provider_proof(tmp_path):
    (tmp_path / "response-0001.json").write_text(json.dumps(meta()))
    (tmp_path / "gateway.jsonl").write_text("\n".join(json.dumps(r) for r in base()))
    with patch(
        "evidence.project_audio",
        return_value={
            "gaps": [{"file": "response-0001.json", "reason": "played_audio_mismatch"}],
            "segments": [],
            "text": None,
        },
    ):
        result = evaluate_evidence(
            tmp_path,
            {
                "protocol": "gaia2-qwen-audio/8",
                "kind": "mixed",
                "requires_answer": True,
            },
            end_reason="idle_window",
        )
    assert result["speech_evidence_status"] == "missing_or_corrupt"


def test_provider_event_id_collision_invalidates_both_responses():
    rows = base()
    other = terminal("new")
    other["payload"]["event_id"] = "done-old"
    rows.append(other)
    assert decision(rows)["decision"] == "unverified_interruption"


def test_malformed_terminal_detail_does_not_crash_or_accept():
    rows = base()
    rows[2]["payload"]["response"]["status_details"] = "invalid"
    assert decision(rows)["decision"] == "unverified_interruption"


def test_exact_duplicate_start_after_cancel_is_not_a_new_lifecycle():
    rows = [{**r, "wall_time": float(i)} for i, r in enumerate(base())]
    rows.append(copy.deepcopy(rows[1]))
    report = classify_interruptions([meta()], rows)
    assert report["responses"][0]["decision"] == "normal_interruption"

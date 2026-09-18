import json
import runpy
import sys
import wave
from pathlib import Path

import pytest

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from evidence import evaluate_evidence, validate_grade
from task_contract import VERSION
from task_driver import TaskDriver, TaskProtocol

from adapters.gaia2.campaign_runner import stops_batch
from harbor.metrics.mean import Mean
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import Verifier


def recording(root, *, complete=False, interrupted=True):
    data = {
        "response_id": "response-1",
        "cancelled": not complete,
        "played_completely": complete,
        "played_samples": 100,
        "received_samples": 100 if complete else 300,
        "heard_transcript": "Answer" if complete else None,
        "transcript": "Answer and more",
        "playback_end_sec": 2 if complete else None,
        "declared_kind": "progress",
    }
    (root / "response-0001.json").write_text(json.dumps(data))
    with wave.open(str(root / "response-0001.wav"), "wb") as out:
        out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        out.writeframes(b"\0\0" * 100)
    events = [{"type": "playback.started", "responseId": "response-1", "wall_time": 1}]
    if interrupted:
        events.extend(
            [
                {
                    "type": "playback.clear",
                    "reason": "user_interruption",
                    "wall_time": 2,
                },
                {
                    "type": "playback.cancelled",
                    "responseId": "response-1",
                    "wall_time": 2.01,
                },
            ]
        )
    (root / "gateway.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def contract(kind):
    return {"kind": kind, "requires_answer": kind != "action_only"}


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("action_only", "scored"),
        ("answer_only", "unresolved_speech"),
        ("mixed", "unresolved_speech"),
    ],
)
def test_normal_interruption_affects_only_required_speech(tmp_path, kind, expected):
    recording(tmp_path)
    result = evaluate_evidence(tmp_path, contract(kind))
    assert result["speech_evidence_status"] == "partial_interruption"
    assert result["task_score_status"] == expected
    assert result["task_submission_text"] is None


def test_complete_later_answer_cannot_erase_unknown_speech(tmp_path):
    recording(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    recording(other, complete=True)
    metadata = json.loads((other / "response-0001.json").read_text())
    metadata["response_id"] = "later"
    (tmp_path / "response-0002.json").write_text(json.dumps(metadata))
    (tmp_path / "response-0002.wav").write_bytes(
        (other / "response-0001.wav").read_bytes()
    )
    assert (
        evaluate_evidence(tmp_path, contract("answer_only"))["task_score_status"]
        == "unresolved_speech"
    )


@pytest.mark.parametrize("kind", ["action_only", "answer_only", "mixed"])
def test_corrupt_or_unexplained_partial_audio_is_still_a_hard_failure(tmp_path, kind):
    recording(tmp_path, interrupted=False)
    result = evaluate_evidence(tmp_path, contract(kind))
    assert result["speech_evidence_status"] == "missing_or_corrupt"
    assert result["task_score_status"] == "invalid_execution"


def test_no_speech_is_known_evidence_not_unknown(tmp_path):
    result = evaluate_evidence(tmp_path, contract("answer_only"))
    assert result["speech_evidence_status"] == "complete"
    assert result["task_score_status"] == "scored"
    assert result["task_submission_text"] == ""


def test_budget_failure_is_not_replaced_by_speech_uncertainty(tmp_path):
    recording(tmp_path)
    assert (
        evaluate_evidence(tmp_path, contract("mixed"), end_reason="total_timeout")[
            "task_score_status"
        ]
        == "scored"
    )


def unresolved():
    return {
        "protocol": VERSION,
        "status": "completed_unscored",
        "success": None,
        "measurement_valid": None,
        "task_score_status": "unresolved_speech",
        "speech_evidence_status": "partial_interruption",
        "boundary_responses_calls": 0,
        "judge_responses_calls": 0,
    }


def test_unresolved_is_admitted_without_task_reward():
    assert validate_grade(unresolved()) == {}
    assert not stops_batch(unresolved())
    result = {**unresolved(), "measurement_valid": False}
    assert stops_batch(result)
    assert stops_batch({**unresolved(), "judge_responses_calls": 1})


def test_empty_reward_is_accepted_by_real_harbor_and_not_averaged_as_zero(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / "reward.json"
    path.write_text("{}")
    verifier = object.__new__(Verifier)
    verifier.trial_paths = SimpleNamespace(reward_json_path=path)
    rewards = verifier._parse_reward_json()
    assert VerifierResult(rewards=rewards).rewards == {}
    # Harbor's default mean imputes missing values as zero; v5 must opt out.
    assert Mean().compute([{"reward": 1}, rewards, {"reward": 0}])["mean"] != 0.5
    from adapters.gaia2.task_metric import compute

    result = compute([{"reward": 1}, rewards, {"reward": 0}])
    assert result["task_pass_rate"] == 0.5
    assert result["score_coverage"] == 2 / 3
    assert "task_pass_rate" not in compute([rewards])


def test_actual_task_verifier_writes_empty_reward_and_keeps_status(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[3]
    original_read = Path.read_text
    original_write = Path.write_text
    outputs = {}

    def read(path, *args, **kwargs):
        if str(path) == "/evaluation/results/result.json":
            return json.dumps(unresolved())
        return original_read(path, *args, **kwargs)

    def write(path, text, *args, **kwargs):
        if str(path).startswith("/logs/verifier/"):
            outputs[path.name] = text
            return len(text)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    runpy.run_path(str(root / "adapters/gaia2/qwen_audio_template/tests/verify.py"))
    assert json.loads(outputs["reward.json"]) == {}
    assert "reward.txt" not in outputs
    assert json.loads(outputs["gaia2_qwen_audio_result.json"])["success"] is None


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("action_only", "scored"),
        ("answer_only", "unresolved_speech"),
        ("mixed", "unresolved_speech"),
    ],
)
def test_task_driver_does_not_submit_partial_text_or_use_a_model(
    tmp_path, kind, expected
):
    recording(tmp_path)
    protocol = TaskProtocol(tmp_path, lambda: (1, 1))
    driver = TaskDriver(
        "http://unused",
        "token",
        tmp_path / "input.wav",
        tmp_path,
        list,
        lambda: (1, 1),
        lambda _: pytest.fail("No AUI submission permitted"),
        protocol=protocol,
        contract=contract(kind),
    )
    protocol.end_reason = "idle_window"
    driver.finalize_projection()
    assert driver.task_evidence["task_score_status"] == expected
    assert protocol.turn == 0
    assert (tmp_path / "response-0001.wav").exists()


def test_real_harbor_uv_metric_preserves_unknown(tmp_path, monkeypatch):
    import os
    import shutil

    from harbor.metrics.uv_script import UvScript

    binary = shutil.which("uv") or "/tmp/gaia2-tools/bin/uv"
    if not Path(binary).is_file():
        pytest.skip("uv executable required for real metric integration")
    monkeypatch.setenv(
        "PATH", str(Path(binary).parent) + os.pathsep + os.environ.get("PATH", "")
    )
    script = Path(__file__).resolve().parents[3] / "adapters/gaia2/task_metric.py"
    result = UvScript(script).compute([{"reward": 1}, {}, {"reward": 0}])
    assert result["task_pass_rate"] == 0.5
    assert result["unresolved_count"] == 1
    assert result["scored_count"] == 2


@pytest.mark.parametrize(
    "protocol_version",
    [VERSION, "gaia2-qwen-audio/6", "gaia2-qwen-audio/7", "gaia2-qwen-audio/8"],
)
def test_v5_scheduler_continues_after_unknown_and_stops_at_five(
    tmp_path, monkeypatch, protocol_version
):
    from test_gaia2_window import campaign

    from adapters.gaia2 import campaign_runner as runner

    launched = campaign(tmp_path, monkeypatch)
    manifest = runner.verify_freeze(tmp_path)
    manifest.update(
        protocol=protocol_version, metric_uv_executable="/tmp/gaia2-tools/bin/uv"
    )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    normal = {
        **unresolved(),
        "status": "completed",
        "success": True,
        "measurement_valid": True,
        "task_score_status": "scored",
        "speech_evidence_status": "partial_interruption",
    }
    outcomes = [
        normal,
        unresolved(),
        {**normal, "success": False},
        normal,
        unresolved(),
    ]

    def results(job):
        index = int(job.name.rsplit("-", 1)[-1])
        return {
            f"case_{index}": {
                **outcomes[index],
                "audio": {"source_id": f"case_{index}"},
            }
        }

    monkeypatch.setattr(runner, "window_results", results)
    ledger = runner.run(tmp_path, "audio", True, max_attempts=5)
    assert len(launched) == 5
    assert ledger["state"] == "attempt_limit_reached"
    assert ledger["requested_attempt_limit"] == 5
    report = json.loads((tmp_path / "PROGRESS.json").read_text())
    assert report["unresolved_task_scores"] == 2
    assert report["verified_score_denominator"] == 3
    assert report["verified_pass_rate"] == 2 / 3
    runner.run(tmp_path, "audio", True, max_attempts=5)
    assert len(launched) == 5


def test_unresolved_audit_passes_without_claiming_official_replay(tmp_path):
    import hashlib

    from adapters.gaia2.window_audit import audit_trial, verify_saved_audit

    root = tmp_path / "jobs/job/trial/artifacts/evaluation/results"
    root.mkdir(parents=True)
    recording(root)
    required = contract("answer_only")
    frozen = tmp_path / "dataset/task/environment/task-contract.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(json.dumps(required))
    (root / "task-contract.json").write_text(json.dumps(required))
    result = {
        **unresolved(),
        "task_contract": required,
        "timing": {"observation_end_reason": "idle_window"},
    }
    (root / "result.json").write_text(json.dumps(result))
    (root / "projection.json").write_text(
        json.dumps(evaluate_evidence(root, required, end_reason="idle_window"))
    )
    (root / "integrity.json").write_text(
        json.dumps(
            {
                "files": {
                    f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in root.iterdir()
                }
            }
        )
    )
    row = {"source_id": "answer", "task_name": "task"}
    report = audit_trial(tmp_path, row, tmp_path / "jobs/job", result)
    assert report["passed"] and report["admission_allowed"]
    assert report["official_replay"] != True
    entry = {
        **row,
        "job": str(tmp_path / "jobs/job"),
        "result": result,
        "audit": report,
    }
    verify_saved_audit(tmp_path, entry)
    (root / "response-0001.wav").write_bytes(b"damaged")
    with pytest.raises(ValueError):
        verify_saved_audit(tmp_path, entry)


def test_contract_mount_is_evaluator_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import yaml

    from adapters.gaia2 import contract_packaging

    folder = tmp_path / "task/environment"
    folder.mkdir(parents=True)
    (folder / "scenario.json").write_text("{}")
    (folder / "task-contract.json").write_text(json.dumps(contract("action_only")))
    compose = folder / "docker-compose.yaml"
    compose.write_text(
        yaml.safe_dump(
            {"services": {"main": {"image": "agent"}, "are-runner": {"volumes": []}}}
        )
    )
    monkeypatch.setattr(
        contract_packaging.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0),
    )
    contract_packaging.build_contracts(tmp_path)
    parsed = yaml.safe_load(compose.read_text())
    assert "volumes" not in parsed["services"]["main"]
    assert parsed["services"]["are-runner"]["volumes"] == [
        "${CONTEXT_DIR}/task-contract.json:/evaluation/task-contract.json:ro"
    ]

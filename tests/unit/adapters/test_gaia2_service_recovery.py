import copy
import json
from pathlib import Path

import pytest
import yaml

from adapters.gaia2 import campaign_runner as runner
from adapters.gaia2.evaluation_campaign import digest, write_json


def recovery_case(tmp_path, monkeypatch):
    manifest = {
        "protocol": "gaia2-qwen-audio/3",
        "source": {"source_sha256": "frozen"},
        "eligible": [
            {"source_id": "case_1", "task_name": "task-1", "sha256": "audio-hash"}
        ],
    }
    write_json(tmp_path / "manifest.json", manifest)
    monkeypatch.setattr(runner, "verify_freeze", lambda _: manifest)
    (tmp_path / "audio.yaml").write_text(
        yaml.safe_dump(
            {
                "job_name": "primary",
                "jobs_dir": str(tmp_path / "jobs"),
                "agents": [
                    {
                        "model_name": "same-model",
                        "kwargs": {"env_file": "/private/profile.env"},
                    }
                ],
                "datasets": [{"path": "/frozen/tasks"}],
            }
        )
    )
    original = {
        "status": "infrastructure_error",
        "failure_cause": "provider_asr_failure",
        "success": None,
        "measurement_valid": False,
        "track": "audio",
        "protocol": manifest["protocol"],
        "source": manifest["source"],
        "models": {"model": "same-model"},
        "audio": {"source_id": "case_1", "sha256": "audio-hash"},
    }
    job = tmp_path / "jobs/primary"
    write_json(job / "trial/agent/gaia2_qwen_audio_result.json", original)
    entry = {
        "source_id": "case_1",
        "track": "audio",
        "job": str(job),
        "result": original,
    }
    write_json(
        tmp_path / "execution-ledger.json",
        {
            "manifest_sha256": digest(tmp_path / "manifest.json"),
            "results": [entry],
            "interrupted": [],
        },
    )
    config_path = runner.prepare_infrastructure_retry(tmp_path, "case_1")
    config = yaml.safe_load(config_path.read_text())
    retry = {
        **copy.deepcopy(original),
        "status": "completed",
        "failure_cause": None,
        "success": False,
        "measurement_valid": True,
    }
    result_path = (
        Path(config["jobs_dir"])
        / config["job_name"]
        / "trial/agent/gaia2_qwen_audio_result.json"
    )
    write_json(result_path, retry)
    return manifest, entry, config_path, result_path


def test_service_recovery_accepts_a_model_failure_without_replacing_primary_score(
    tmp_path, monkeypatch
):
    manifest, entry, _, result = recovery_case(tmp_path, monkeypatch)
    before = (tmp_path / "execution-ledger.json").read_bytes()
    runner.accept_infrastructure_retry(tmp_path, "case_1")
    assert runner.reviewed_infrastructure(tmp_path, entry, manifest)
    assert (tmp_path / "execution-ledger.json").read_bytes() == before
    assert entry["result"]["measurement_valid"] is False
    assert entry["result"]["success"] is None
    retry = json.loads(result.read_text())
    retry["success"] = True
    write_json(result, retry)
    with pytest.raises(ValueError, match="changed"):
        runner.reviewed_infrastructure(tmp_path, entry, manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("measurement_valid", False),
        ("models", {"model": "different"}),
        ("source", {"source_sha256": "different"}),
        ("audio", {"source_id": "case_1", "sha256": "different"}),
    ],
)
def test_invalid_or_changed_retry_cannot_reopen_admission(
    tmp_path, monkeypatch, field, value
):
    _, _, _, result = recovery_case(tmp_path, monkeypatch)
    retry = json.loads(result.read_text())
    retry[field] = value
    write_json(result, retry)
    with pytest.raises(ValueError, match="service recovery"):
        runner.accept_infrastructure_retry(tmp_path, "case_1")


def test_retry_configuration_and_one_attempt_limit_are_enforced(tmp_path, monkeypatch):
    _, _, config_path, _ = recovery_case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="already"):
        runner.prepare_infrastructure_retry(tmp_path, "case_1")
    config = yaml.safe_load(config_path.read_text())
    config["agents"][0]["model_name"] = "other-model"
    config_path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="provenance"):
        runner.accept_infrastructure_retry(tmp_path, "case_1")


def test_model_failures_cannot_be_relabelled_as_retryable_service_failures(
    tmp_path, monkeypatch
):
    recovery_case(tmp_path, monkeypatch)
    ledger_path = tmp_path / "execution-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    ledger["results"][0]["result"].update(
        status="completed", success=False, measurement_valid=True
    )
    write_json(ledger_path, ledger)
    with pytest.raises(ValueError, match="Exactly one"):
        runner.prepare_infrastructure_retry(tmp_path, "case_1")

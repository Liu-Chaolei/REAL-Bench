import asyncio
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from model_proxy import make_app
from observation import IdleWindow, eligibility, project_audio
from window_driver import WindowDriver, WindowProtocol

from adapters.gaia2 import campaign_runner as runner
from adapters.gaia2.evaluation_campaign import digest, write_json


def audio(root, number, text, *, complete=True, end=1, kind="progress"):
    path = root / f"response-{number:04d}"
    with wave.open(str(path.with_suffix(".wav")), "wb") as out:
        out.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        out.writeframes(b"\0\0" * 100)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "response_id": f"r{number}",
                "played_samples": 100,
                "played_completely": complete,
                "heard_transcript": text if complete else None,
                "transcript": text,
                "playback_end_sec": end,
                "declared_kind": kind,
            }
        )
    )


def test_projection_preserves_all_speech_in_actual_playback_order(tmp_path):
    audio(tmp_path, 1, "The booking has been submitted.", end=64, kind="progress")
    audio(tmp_path, 2, "Actually still working.", end=120, kind="checkpoint")
    audio(tmp_path, 3, "Done.", end=190, kind="final")
    result = project_audio(tmp_path)
    assert (
        result["text"]
        == "The booking has been submitted.\n\nActually still working.\n\nDone."
    )
    assert result["audible_transcript_complete"]
    for row in result["segments"]:
        assert result["text"][row["text_start"] : row["text_end"]] == row["text"]


def test_partial_speech_never_becomes_a_complete_score(tmp_path):
    audio(tmp_path, 1, "Not all of this was heard", complete=False)
    audio(tmp_path, 2, "Correct final answer", kind="final")
    result = project_audio(tmp_path)
    assert result["text"] is None
    assert result["audible_transcript_complete"] is False
    assert result["gaps"][0]["reason"] == "audible_transcript_incomplete"


def test_silence_does_not_synthesize_an_answer(tmp_path):
    assert project_audio(tmp_path)["text"] == ""
    assert project_audio(tmp_path)["segments"] == []


def test_audio_sample_mismatch_is_not_silently_accepted(tmp_path):
    audio(tmp_path, 1, "spoken")
    path = tmp_path / "response-0001.json"
    row = json.loads(path.read_text())
    row["played_samples"] += 1
    path.write_text(json.dumps(row))
    assert not project_audio(tmp_path)["audible_transcript_complete"]


@pytest.mark.parametrize("activity_at", [0, 1, 14.99, 15])
def test_idle_window_restarts_for_real_activity(activity_at):
    window = IdleWindow(15)
    assert not window.update(0, idle=True, activity=0)
    assert not window.update(activity_at, idle=True, activity=1)
    assert not window.update(activity_at + 14.99, idle=True, activity=1)
    assert window.update(activity_at + 15, idle=True, activity=1)


def test_pending_work_blocks_idle_even_when_model_claims_completion():
    window = IdleWindow()
    assert not window.update(0, idle=True, activity=0)
    assert not window.update(20, idle=False, activity=0)
    assert not window.update(30, idle=True, activity=0)
    assert window.update(45, idle=True, activity=0)


@pytest.mark.parametrize(
    "extra",
    [
        {"event_type": "USER"},
        {"event_type": "ENV"},
        {"event_type": "CONDITION"},
    ],
)
def test_unsupported_scenarios_are_not_replaced(extra):
    assert not eligibility({"events": [{"event_type": "USER"}, extra]})["supported"]


async def test_receipts_do_not_wait_for_submission_or_call_a_boundary_model(tmp_path):
    p = WindowProtocol(tmp_path, lambda: (0, 1))
    p.start()
    delivery = p.bind("response", "delivery", "final")
    delivery.played = True
    delivery.playback_end_sec = 42

    async def forbidden(*args):
        pytest.fail("Playback receipt must not invoke official barrier")

    assert await p.commit(delivery, "Claimed done", forbidden, forbidden)
    assert p.turn == 0
    assert p.delivery_status("delivery")["committed"]
    assert p.metrics()["last_audio_playback_end_sec"] == 42
    assert "final_formal_delivery_sec" not in p.metrics()
    assert not await p.commit(delivery, "Claimed done", forbidden, forbidden)
    with pytest.raises(ValueError):
        await p.commit(delivery, "Changed answer", forbidden, forbidden)


@pytest.mark.parametrize(
    "pending",
    [
        "pendingTasks",
        "pendingPlayback",
        "queuedResponses",
        "activeResponses",
        "userSpeaking",
    ],
)
def test_lifecycle_checks_all_pending_work(tmp_path, pending):
    p = WindowProtocol(tmp_path, lambda: (0, 1))
    driver = WindowDriver(
        "http://gateway",
        "token",
        tmp_path / "in.wav",
        tmp_path,
        list,
        lambda: (0, 1),
        lambda text: None,
        protocol=p,
    )
    driver.input_done = True
    driver.runtime = SimpleNamespace(waiting=False, operations=asyncio.Lock())
    assert driver.locally_idle({"ready": True}, {"active": 0})
    assert not driver.locally_idle({"ready": True, pending: 1}, {"active": 0})
    assert not driver.locally_idle({"ready": True}, {"active": 1})
    driver.pending_input_items.add("asr")
    assert not driver.locally_idle({"ready": True}, {"active": 0})


async def test_proxy_state_is_authenticated_and_does_not_create_activity(tmp_path):
    app = make_app("http://unused", "secret", "gpt", "low", "control", tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://proxy"
    ) as client:
        assert (await client.get("/__state")).status_code == 401
        for _ in range(2):
            result = await client.get(
                "/__state", headers={"Authorization": "Bearer control"}
            )
            assert result.json() == {"active": 0, "sequence": 0, "sealed": False}
    assert not list(tmp_path.iterdir())


def campaign(tmp_path, monkeypatch, *, failed_audit=False, stop_after=False):
    rows = [
        {
            "source_id": f"case_{i}",
            "task_name": f"task_{i}",
            "protocol_support": {"supported": True},
        }
        for i in range(20)
    ]
    manifest = {
        "protocol": "gaia2-qwen-audio/4",
        "adapter_files": {},
        "seed_cases": rows,
        "attempt_limit": 15,
    }
    write_json(tmp_path / "manifest.json", manifest)
    proof = tmp_path / "proof.log"
    proof.write_text("deterministic checks")
    write_json(
        tmp_path / "observation-validation.json",
        {
            "passed": True,
            "adapter_files_sha256": runner.value_digest({}),
            "artifacts": [{"path": str(proof), "sha256": digest(proof)}],
        },
    )
    monkeypatch.setattr(runner, "verify_freeze", lambda _: manifest)
    (tmp_path / "audio.yaml").write_text(
        yaml.safe_dump(
            {"jobs_dir": str(tmp_path / "jobs"), "datasets": [{"path": "dataset"}]}
        )
    )
    launched = []

    class Process:
        def __init__(self, command, **kwargs):
            config = yaml.safe_load(Path(command[-1]).read_text())
            index = len(launched)
            launched.append(index)
            self.pid = 999
            result = {
                "status": "completed",
                "success": False,
                "measurement_valid": True,
                "audio": {"source_id": f"case_{index}"},
            }
            write_json(
                Path(config["jobs_dir"])
                / config["job_name"]
                / "trial/agent/gaia2_qwen_audio_result.json",
                result,
            )
            if stop_after:
                (tmp_path / "stop-after-current").touch()

        def poll(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    from adapters.gaia2 import window_audit

    monkeypatch.setattr(window_audit, "verify_saved_audit", lambda *args: None)
    monkeypatch.setattr(
        window_audit, "audit_trial", lambda *args: {"passed": not failed_audit}
    )
    return launched


def test_fifteen_attempt_cap_never_launches_sixteenth_and_counts_resume(
    tmp_path, monkeypatch
):
    launched = campaign(tmp_path, monkeypatch)
    result = runner.run(tmp_path, "audio", True)
    assert len(launched) == 15
    assert result["state"] == "attempt_limit_reached"
    result = runner.run(tmp_path, "audio", True)
    assert len(launched) == 15


def test_audit_failure_stops_before_next_attempt(tmp_path, monkeypatch):
    launched = campaign(tmp_path, monkeypatch, failed_audit=True)
    result = runner.run(tmp_path, "audio", True)
    assert len(launched) == 1
    assert result["state"] == "blocked_on_measurement_or_service"
    with pytest.raises(ValueError, match="audit"):
        runner.run(tmp_path, "audio", True)


def test_stop_after_current_preserves_result_and_audit(tmp_path, monkeypatch):
    launched = campaign(tmp_path, monkeypatch, stop_after=True)
    result = runner.run(tmp_path, "audio", True)
    assert len(launched) == 1
    assert result["results"][0]["audit"]["passed"]
    assert result["state"] == "stopped_after_current"


def test_orphan_played_audio_cannot_disappear_from_projection(tmp_path):
    audio(tmp_path, 1, "spoken")
    (tmp_path / "response-0001.json").unlink()
    assert not project_audio(tmp_path)["audible_transcript_complete"]


def test_false_complete_flag_does_not_hide_unplayed_samples(tmp_path):
    audio(tmp_path, 1, "not fully played")
    path = tmp_path / "response-0001.json"
    row = json.loads(path.read_text())
    row["received_samples"] = row["played_samples"] + 20
    path.write_text(json.dumps(row))
    assert not project_audio(tmp_path)["audible_transcript_complete"]


async def test_unsupported_host_preflight_precedes_gateway_setup():
    from unittest.mock import AsyncMock

    from adapters.gaia2.qwen_audio_agent import Gaia2QwenAudioAgent

    agent = object.__new__(Gaia2QwenAudioAgent)
    agent.protocol_version = "gaia2-qwen-audio/4"
    agent.preflight = lambda: None
    environment = SimpleNamespace(
        service_exec=AsyncMock(
            return_value=SimpleNamespace(
                return_code=1, stdout="dynamic_environment_not_supported"
            )
        ),
        exec=AsyncMock(),
    )
    with pytest.raises(ValueError, match="unsupported_protocol"):
        await agent.setup(environment)
    environment.exec.assert_not_called()
    assert environment.service_exec.await_count == 1

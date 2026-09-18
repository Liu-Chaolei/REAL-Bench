import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from audio_driver import InfrastructureError
from native_recovery import WATCHDOG_MESSAGE, NativeRecovery
from passive_driver import PassiveDriver, PassiveProtocol


def driver(tmp_path):
    clock = [0.0]
    protocol = PassiveProtocol(tmp_path, lambda: (clock[0], 1))
    protocol.start()
    d = PassiveDriver(
        "http://unused",
        "token",
        tmp_path / "input.wav",
        tmp_path,
        list,
        protocol.clock,
        lambda _: None,
        protocol=protocol,
        contract={
            "protocol": "gaia2-qwen-audio/6",
            "kind": "action_only",
            "requires_answer": False,
        },
    )
    d.input_done = True
    d.user_context = ["Reschedule Thursday events and notify attendees."]
    d.runtime = SimpleNamespace(waiting=False, operations=asyncio.Lock())
    d.control = AsyncMock(side_effect=AssertionError("No recovery commands allowed"))
    d.send = AsyncMock(side_effect=AssertionError("No injected inputs allowed"))
    return d, clock


async def test_0361_watchdog_is_observed_without_ending_or_repairing(tmp_path):
    d, clock = driver(tmp_path)
    clock[0] = 33.5035
    assert await d.observe_gateway_event({"type": "error", "message": WATCHDOG_MESSAGE})
    assert d.native_recovery.pending and not d.done
    assert d.recovering_since is None  # the old 30-second timeout is not armed
    assert d.transport_gate.is_set()  # no microphone/playback pause
    assert d.retry_deliveries == set()
    clock[0] = 100
    assert not d.locally_idle({"ready": True}, {"active": 0})
    assert d.native_recovery.confirm({"ready": True}, clock[0]) is None
    await d.observe_gateway_event({"type": "voice.connection", "state": "connecting"})
    await d.observe_gateway_event({"type": "voice.connection", "state": "connected"})
    assert d.native_recovery.confirm({"ready": False}, clock[0]) is None
    assert d.native_recovery.confirm({"ready": True}, clock[0])["outcome"] == "ready"
    assert d.locally_idle({"ready": True}, {"active": 0})
    d.control.assert_not_called()
    d.send.assert_not_called()


@pytest.mark.parametrize("case", ["input", "asr", "transcript", "playback", "delivery"])
async def test_uncovered_states_are_not_scored_as_agent_errors(tmp_path, case):
    d, _ = driver(tmp_path)
    if case == "input":
        d.input_done = False
    if case == "asr":
        d.pending_input_items.add("unconfirmed")
    if case == "transcript":
        d.user_context = []
    if case == "playback":
        d.responses["r"] = SimpleNamespace(
            cancelled=False, recorded=False, started=True, pcm=b"aa"
        )
    if case == "delivery":
        d.delivery_waiters["d"] = asyncio.get_running_loop().create_future()
    with pytest.raises(InfrastructureError) as e:
        await d.observe_gateway_event({"type": "error", "message": WATCHDOG_MESSAGE})
    assert e.value.cause == "unsupported_recovery_state"
    assert not d.native_recovery.pending


@pytest.mark.parametrize(
    "event,cause",
    [
        ({"type": "error", "message": "account in arrears"}, "provider_billing"),
        (
            {"type": "error", "message": "HTTP 503 service unavailable"},
            "provider_unavailable",
        ),
        ({"type": "error", "http_status": 429}, "provider_rate_limit"),
        (
            {
                "type": "evaluation.provider_event",
                "payload": {"type": "error", "error": {"code": "invalid_api_key"}},
            },
            "provider_unavailable",
        ),
        (
            {"type": "voice.connection", "state": "unavailable", "message": "欠费"},
            "provider_billing",
        ),
    ],
)
async def test_external_faults_are_never_swallowed_by_recovery(tmp_path, event, cause):
    d, _ = driver(tmp_path)
    await d.observe_gateway_event({"type": "error", "message": WATCHDOG_MESSAGE})
    with pytest.raises(InfrastructureError) as e:
        await d.observe_gateway_event(event)
    assert e.value.cause == cause


async def test_unknown_errors_keep_unknown_attribution(tmp_path):
    d, _ = driver(tmp_path)
    with pytest.raises(InfrastructureError) as e:
        await d.observe_gateway_event(
            {"type": "error", "message": "Something unclassified"}
        )
    assert e.value.cause == "unclassified_gateway_error"
    assert not await d.observe_gateway_event(
        {"type": "transcript.final", "role": "assistant", "content": WATCHDOG_MESSAGE}
    )


async def test_legacy_repair_commands_are_forbidden(tmp_path):
    d, _ = driver(tmp_path)
    with pytest.raises(InfrastructureError):
        d.begin_recovery()
    with pytest.raises(InfrastructureError):
        await d.restore_deliveries()
    d.control.assert_not_called()


def test_duplicate_errors_and_reconnects_do_not_refresh_the_budget():
    r = NativeRecovery()
    assert r.start(33.5)
    r.connection({"type": "voice.ready"}, 40)
    assert not r.start(50)
    assert r.confirm({"ready": True}, 51) is None
    r.connection({"type": "voice.connection", "state": "connected"}, 55)
    r.connection({"type": "voice.connection", "state": "unavailable"}, 56)
    assert r.confirm({"ready": True}, 57) is None
    r.connection({"type": "voice.ready"}, 90)
    assert r.confirm({"ready": True}, 91)["started_sec"] == 33.5
    assert r.summary(100)["duration_sec"] == 57.5
    assert r.summary(100)["count"] == 1


def test_unfinished_recovery_is_archived_at_original_budget():
    r = NativeRecovery()
    r.start(33.5)
    r.finish("total_timeout", 1800)
    assert r.summary(1800)["completed_count"] == 0
    assert r.summary(1800)["episodes"][0]["outcome"] == "total_timeout"
    assert r.summary(1900)["duration_sec"] == 1766.5


async def test_connection_events_do_not_trigger_adapter_replay(tmp_path):
    d, _ = driver(tmp_path)
    await d.observe_gateway_event({"type": "error", "message": WATCHDOG_MESSAGE})
    for event in [
        {"type": "voice.connection", "state": "unavailable"},
        {"type": "evaluation.connection.recovering", "reason": "inactivity"},
        {"type": "voice.connection", "state": "connecting"},
        {"type": "voice.ready", "inputSampleRate": 16000},
    ]:
        assert await d.observe_gateway_event(event)
    assert d.retry_deliveries == set() and d.transport_gate.is_set()
    d.send.assert_not_called()
    d.control.assert_not_called()


async def test_recorded_0361_trace_reaches_watchdog_without_an_exception(tmp_path):
    root = Path(__file__).resolve().parents[3]
    jobs = root / "datasets/gaia2-campaign-v5-task-evidence-20260911-smoke5/jobs"
    trace = next(jobs.glob("*/*/artifacts/evaluation/results/gateway.jsonl"), None)
    if trace is None:
        pytest.skip("Historical 0361 fixture unavailable")
    d, _clock = driver(tmp_path)
    for line in trace.read_text().splitlines():
        event = json.loads(line)
        if event["direction"] == "server":
            await d.observe_gateway_event(event)
    assert d.native_recovery.pending
    assert d.protocol.metrics()["native_recovery"]["count"] == 1
    assert d.observation.end_reason is None


async def test_ready_snapshot_must_be_queried_after_new_connection(tmp_path):
    d, clock = driver(tmp_path)
    await d.observe_gateway_event({"type": "error", "message": WATCHDOG_MESSAGE})
    clock[0] = 40
    await d.observe_gateway_event({"type": "voice.connection", "state": "connected"})
    d.control = AsyncMock(return_value={"ready": False})
    d.proxy_state = AsyncMock(return_value={"active": 0, "sequence": 0})
    assert not await d.advance_observation({"ready": True})  # stale caller snapshot
    d.control.assert_awaited_once_with("state")
    assert d.native_recovery.pending
    d.control = AsyncMock(return_value={"ready": True})
    clock[0] = 41
    assert not await d.advance_observation({"ready": False})
    assert not d.native_recovery.pending
    assert d.recoveries == 1


async def test_abort_reports_not_collected_instead_of_corruption(tmp_path):
    import hashlib

    from adapters.gaia2.window_audit import audit_trial

    root = tmp_path / "jobs/job/trial/artifacts/evaluation/results"
    root.mkdir(parents=True)
    result = {
        "protocol": "gaia2-qwen-audio/6",
        "status": "infrastructure_error",
        "success": None,
        "measurement_valid": False,
        "speech_evidence_status": "not_collected",
        "failure_origin": "unknown_gateway_error",
        "failure_cause": "unclassified_gateway_error",
    }
    raw = json.dumps(result)
    (root / "result.json").write_text(raw)
    (root / "integrity.json").write_text(
        json.dumps({"files": {"result.json": hashlib.sha256(raw.encode()).hexdigest()}})
    )
    report = audit_trial(
        tmp_path,
        {"source_id": "case", "task_name": "task"},
        tmp_path / "jobs/job",
        result,
    )
    assert report["artifact_integrity"] is True
    assert report["speech_evidence_status"] == "not_collected"
    assert not report["passed"]
    assert "unclassified_gateway_error" in report["error"]

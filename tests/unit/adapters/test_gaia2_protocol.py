"""Controls for the voice variant: never use an oracle to classify delivery."""

import asyncio
import json

import pytest

from adapters.gaia2.qwen_audio_template.environment.runner.protocol import (
    KINDS,
    Protocol,
    ProtocolError,
)


@pytest.fixture
def protocol(tmp_path):
    now = [100.0]
    protocol = Protocol(tmp_path, lambda: (now[0], 1))
    protocol.test_now = now
    protocol.start()
    return protocol


async def barrier(reply, transcript):
    reply(transcript)


async def deliver(
    p, kind, identity="d1", transcript="answer", played=True, cancelled=False
):
    d = p.bind(identity, identity, kind)
    d.played, d.cancelled = played, cancelled
    calls = []
    await p.commit(d, transcript, calls.append, barrier)
    return calls


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_only_formal_delivery_advances_are(protocol, kind):
    calls = await deliver(protocol, kind)
    assert calls == ([] if kind == "progress" else ["answer"])
    assert protocol.turn == (0 if kind == "progress" else 1)


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_interruption_never_commits(protocol, kind):
    assert await deliver(protocol, kind, cancelled=True) == []
    assert protocol.turn == 0


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_unplayed_never_commits(protocol, kind):
    assert await deliver(protocol, kind, played=False) == []


async def test_progress_does_not_change_formal_turn_sequence(protocol):
    for index in range(10):
        await deliver(protocol, "progress", str(index))
    await deliver(protocol, "checkpoint", "checkpoint")
    await deliver(protocol, "final", "final")
    rows = [
        json.loads(line)
        for line in (protocol.output / "protocol.jsonl").read_text().splitlines()
    ]
    assert [
        r["are_turn"]
        for r in rows
        if r["event"] == "delivery.committed" and r["kind"] != "progress"
    ] == [1, 2]


async def test_wrong_formal_reply_cannot_be_erased(protocol):
    calls = await deliver(protocol, "final", transcript="wrong")
    calls += await deliver(protocol, "final", identity="second", transcript="correct")
    assert calls == ["wrong", "correct"]
    assert protocol.turn == 2


async def test_duplicate_delivery_is_idempotent(protocol):
    assert await deliver(protocol, "checkpoint") == ["answer"]
    assert await deliver(protocol, "checkpoint") == []
    assert protocol.turn == 1


async def test_duplicate_changed_text_rejected(protocol):
    await deliver(protocol, "checkpoint")
    with pytest.raises(ProtocolError, match="changed"):
        await deliver(protocol, "checkpoint", transcript="different")


@pytest.mark.parametrize("kind", ["final", "progress"])
def test_reclassification_rejected(protocol, kind):
    protocol.declare("d", "checkpoint")
    with pytest.raises(ProtocolError):
        protocol.declare("d", kind)


def test_cross_request_delivery_rejected(protocol):
    with pytest.raises(ProtocolError, match="another request"):
        protocol.declare("d", "final", "other")


def test_response_cannot_be_rebound(protocol):
    protocol.bind("r", "d1", "checkpoint")
    with pytest.raises(ProtocolError):
        protocol.bind("r", "d2", "checkpoint")


def test_delivery_cannot_bind_two_responses(protocol):
    protocol.bind("r1", "d", "checkpoint")
    with pytest.raises(ProtocolError):
        protocol.bind("r2", "d", "checkpoint")


async def test_checkpoint_does_not_close_request(protocol):
    await deliver(protocol, "checkpoint")
    assert not protocol.can_finish(input_done=True, idle=True)
    protocol.set_state("waiting_environment")
    assert not protocol.can_finish(input_done=True, idle=True)


def test_silent_operation_can_complete(protocol):
    protocol.action("Calendar__delete_event", True)
    protocol.set_state("completed")
    assert protocol.can_finish(input_done=True, idle=True)
    assert protocol.metrics()["final_formal_delivery_sec"] is None


@pytest.mark.parametrize(
    "input_done,idle", [(False, True), (True, False), (False, False)]
)
def test_completion_waits_for_input_and_work(protocol, input_done, idle):
    protocol.set_state("completed")
    assert not protocol.can_finish(input_done=input_done, idle=idle)


def test_pending_delivery_blocks_completion(protocol):
    protocol.declare("d", "final")
    protocol.set_state("completed")
    assert not protocol.can_finish(input_done=True, idle=True)


@pytest.mark.parametrize("state", ["waiting_user", "completed", "failed"])
def test_closed_request_cannot_reopen(protocol, state):
    protocol.set_state(state)
    assert protocol.can_finish(input_done=True, idle=True)
    with pytest.raises(ProtocolError):
        protocol.set_state("running")


def test_notifications_have_exactly_once_consumption(protocol):
    message = {"id": "n", "message": "new mail"}
    assert protocol.notify(message)
    assert not protocol.notify(message)
    assert protocol.take_notifications() == [message]
    assert protocol.take_notifications() == []
    with pytest.raises(ProtocolError):
        protocol.notify({"id": "n", "message": "different"})


def test_first_effective_action_excludes_failed_and_system_calls(protocol):
    protocol.test_now[0] += 1
    protocol.action("System__get_current_time", True, application=False)
    protocol.action("Mail__send", False)
    assert protocol.first_action is None
    protocol.test_now[0] += 4
    protocol.action("Mail__read", True)
    assert protocol.first_action == 5


async def test_barrier_finishes_before_projection(protocol):
    reached = asyncio.Event()
    release = asyncio.Event()

    async def held(reply, transcript):
        reached.set()
        await release.wait()
        reply(transcript)

    d = protocol.bind("r", "d", "checkpoint")
    d.played = True
    calls = []
    async with asyncio.TaskGroup() as group:
        task = group.create_task(protocol.commit(d, "answer", calls.append, held))
        await reached.wait()
        assert protocol.turn == 0
        assert not d.committed
        release.set()
    assert task.result()
    assert protocol.turn == 1


async def test_missing_transcript_fails(protocol):
    with pytest.raises(ProtocolError):
        await deliver(protocol, "final", transcript="")


def test_clock_starts_once(protocol):
    with pytest.raises(ProtocolError):
        protocol.start()

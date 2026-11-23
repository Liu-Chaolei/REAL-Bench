"""Small, key-free tests for the shared audio clock and detector."""

import asyncio
import struct

import pytest

from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT
from tau2.voice.audio_activity import MediaPlayback, SpeechDetector, merge_intervals
from tau2.voice.audio_native.adapter import DiscreteTimeAdapter


def test_packet_boundaries_do_not_change_detection():
    pcm = b"\0\0" * 320 + struct.pack("<h", 1000) * 640 + b"\0\0" * 160
    whole = SpeechDetector(16000).feed(pcm, final=True)
    detector, pieces = SpeechDetector(16000), []
    for start in range(0, len(pcm), 74):
        pieces.extend(detector.feed(pcm[start : start + 74]))
    pieces.extend(detector.feed(b"", final=True))
    assert merge_intervals(pieces) == whole == [[0.02, 0.06]]


def test_partial_window_keeps_actual_length():
    assert SpeechDetector(8000).feed(struct.pack("<h", 2000) * 40, final=True) == [
        [0, 0.005]
    ]


def test_receipt_queue_gap_and_response_scoped_cancel():
    now = [10.123]
    playback = MediaPlayback(TELEPHONY_AUDIO_FORMAT, lambda: now[0])
    playback.append(b"\xaf" * 800, "old")
    playback.append(b"\xaf" * 800, "new")
    now[0] += 0.025
    played = playback.drain()
    assert played[0]["start"] == 10.123
    assert played[0]["end"] == pytest.approx(10.148)
    playback.cancel({"old"})
    assert all(r["response_id"] == "new" for r in playback.queue)
    assert playback.queue[0]["start"] == pytest.approx(now[0])
    now[0] += 0.2
    assert playback.drain()[0]["response_id"] == "new"
    now[0] += 0.1
    playback.append(b"\xaf" * 80, "after-gap")
    assert playback.queue[0]["start"] == now[0]


class Adapter(DiscreteTimeAdapter):
    """Protocol stub using the production common media lifecycle."""

    def __init__(self):
        super().__init__(80, send_audio_instant=False)
        self.events = asyncio.Queue()

    def connect(self, *args, **kwargs):
        pass

    def disconnect(self):
        self.clear_buffers()

    @property
    def is_connected(self):
        return True

    def run_tick(self, *args, **kwargs):
        raise NotImplementedError

    async def _flush_pending_tool_results(self):
        pass

    async def _execute_tick(self, user_audio, tick_number, result, tick_start):
        async def receive():
            return [await self.events.get()]

        def handle(result, event):
            if event == "cancel":
                result.agent_audio_chunks.clear()
            else:
                result.agent_audio_chunks.append((event, "response"))

        self._start_media_receiver(receive, handle)

        async def send(data):
            pass

        await self._send_audio_chunked(user_audio, send, 160)


@pytest.mark.asyncio
async def test_delayed_audio_and_idle_receiver_use_event_time():
    adapter = Adapter()

    async def later():
        await asyncio.sleep(0.035)
        adapter.events.put_nowait(b"\xaf" * 800)

    producer = asyncio.create_task(later())
    try:
        first = await adapter._async_run_tick(b"\xff" * 640, 1)
        await producer
        assert first.speech_intervals
        assert first.speech_intervals[0][0] - first.media_origin >= 0.03
        assert first.agent_audio_bytes < 640
        await asyncio.sleep(0.02)
        adapter.events.put_nowait("cancel")
        await asyncio.sleep(0.01)
        assert not adapter.playback.pending_chunks()
        second = await adapter._async_run_tick(b"\xff" * 640, 2)
        assert second.media_timeline  # samples played between ticks remain observable
    finally:
        adapter.disconnect()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_known_user_duration_excludes_padding_and_keeps_pause():
    adapter = Adapter()
    audio = b"\xff" * 640
    adapter._input_context = dict(
        audio=audio, duration=0.08, speech=[0.01, 0.055], utterance_ids=["u"]
    )
    try:
        result = await adapter._async_run_tick(audio, 1)
        pieces = [r for r in result.media_timeline if r["role"] == "user"]
        voiced = [p for r in pieces for p in r["speech_intervals"]]
        assert sum(b - a for a, b in voiced) == pytest.approx(0.045)
        assert len(voiced) > 1  # transport segmentation never infers a sentence end
        assert all(r["utterance_ids"] == ["u"] for r in pieces)
    finally:
        adapter.disconnect()
        await asyncio.sleep(0)


def test_both_metrics_use_precise_shared_evidence(tmp_path):
    import base64
    from types import SimpleNamespace

    from tau2.data_model.diagnostics import DiagnosticEvidence
    from tau2.data_model.message import AssistantMessage, Tick, UserMessage
    from tau2.diagnostics.collect import collect_audio
    from tau2.diagnostics.voice import evaluate_voice
    from tau2.metrics.voice_interaction_metrics import (
        extract_all_segments,
        extract_turn_transitions,
    )

    fmt = TELEPHONY_AUDIO_FORMAT.model_dump(mode="json")
    tick = Tick(
        tick_id=0,
        timestamp="2026-11-21T00:00:00",
        tick_duration_seconds=0.2,
        user_chunk=UserMessage(role="user", content="Question", utterance_ids=["u"]),
        agent_chunk=AssistantMessage(
            role="assistant",
            content="Answer",
            raw_data={
                "media_origin": 10.0,
                "speech_intervals": [[10.075, 10.095]],
                "media_timeline": [
                    dict(
                        role="user",
                        user_event_kind="formal",
                        user_event_version="tau-user-audio-events/1",
                        start=10.005,
                        end=10.055,
                        speech_intervals=[[10.005, 10.055]],
                        utterance_ids=["u"],
                        audio=base64.b64encode(b"\xff" * 400).decode(),
                        format=fmt,
                    ),
                    dict(
                        role="agent",
                        start=10.075,
                        end=10.095,
                        audio=base64.b64encode(b"\xaf" * 160).decode(),
                        format=fmt,
                    ),
                ],
            },
        ),
    )
    user, agent = extract_all_segments([tick])
    transitions = extract_turn_transitions(user, agent)
    assert transitions[0].gap_sec == pytest.approx(0.02)
    audio = collect_audio(SimpleNamespace(ticks=[tick]), tmp_path)
    evidence = DiagnosticEvidence(
        simulation_id="s", task_id="t", domain="mock", audio=audio
    )
    report = evaluate_voice(evidence, tmp_path)
    assert report["metrics"]["response_latency_mean"]["value"] == pytest.approx(0.02)
    assert audio["time_basis"] == "direct_media_events"
    assert report["namespace"] == "voice_interaction"


@pytest.mark.asyncio
async def test_call_end_keeps_played_tail_and_cancels_future():
    adapter = Adapter()
    adapter.events.put_nowait(b"\xaf" * 2400)
    try:
        await adapter._async_run_tick(b"\xff" * 640, 1)
        await asyncio.sleep(0.025)
        tail = await adapter._finish_media()
        assert tail["media_timeline"]
        assert tail["speech_intervals"]
        assert not adapter.playback.pending_chunks()
        assert adapter.playback.played["response"] < 2400
    finally:
        adapter.disconnect()
        await asyncio.sleep(0)

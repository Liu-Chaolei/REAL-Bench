"""Playback, final-confirmation and caller-history contracts without API keys."""

import asyncio
import json

import pytest

from tau2.agent.base.streaming import StreamingState
from tau2.config import DEFAULT_QWEN_ASR_STREAMING_MODEL
from tau2.data_model.message import AssistantMessage
from tau2.data_model.transcript import (
    LiveTranscriptSnapshot,
    LiveTranscriptTick,
    ParticipantTick,
)
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
from tau2.voice.audio_native.qwen_agent.qwen_streaming_transcription import (
    QwenStreamingPlayedAudioTranscriber,
)
from tau2.voice.qwen_asr import qwen_streaming_asr_url


class Socket:
    """A controlled ASR peer with interim and final results sent separately."""

    def __init__(self, peer):
        self.peer = peer
        self.incoming = asyncio.Queue()
        self.task_id = None
        self.audio = []
        self.closed = False

    async def __aenter__(self):
        await self.peer.allow_connect.wait()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.incoming.get())

    def event(self, kind, **payload):
        self.incoming.put_nowait(
            {
                "header": {"event": kind, "task_id": self.task_id},
                "payload": payload,
            }
        )

    def sentence(self, text, *, final=False, end=1000):
        self.event(
            "result-generated",
            output={
                "sentence": {
                    "sentence_id": 1,
                    "text": text,
                    "sentence_end": final,
                    "begin_time": 0,
                    "end_time": end if final else None,
                }
            },
        )

    async def send(self, data):
        if isinstance(data, bytes):
            self.audio.append(data)
            return
        request = json.loads(data)
        self.peer.requests.append(request)
        if request["header"]["action"] == "run-task":
            self.task_id = request["header"]["task_id"]
            self.event("task-started")
        else:
            if self.peer.final_text:
                self.sentence(self.peer.final_text, final=True, end=self.peer.final_end)
            self.event("task-finished")


class Peer:
    """Inject a WebSocket factory rather than calling the provider."""

    def __init__(self):
        self.sockets, self.requests, self.connections = [], [], []
        self.allow_connect = asyncio.Event()
        self.allow_connect.set()
        self.final_text = ""
        self.final_end = 400

    def connect(self, url, **kwargs):
        self.connections.append((url, kwargs))
        socket = Socket(self)
        self.sockets.append(socket)
        return socket


async def until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Background observer did not reach the expected state")


def config(**updates):
    return QwenAgentConfig(
        asr_provider="qwen",
        asr_model=DEFAULT_QWEN_ASR_STREAMING_MODEL,
        asr_api_key_env="QWEN_API_KEY",
        asr_base_url="https://dashscope.invalid",
        **updates,
    )


@pytest.fixture
def peer(monkeypatch):
    value = Peer()
    monkeypatch.setenv("QWEN_API_KEY", "observer-private-key")
    monkeypatch.setattr(
        "tau2.voice.audio_native.qwen_agent.qwen_streaming_transcription.websockets.connect",
        value.connect,
    )
    return value


def test_media_never_waits_for_startup_and_final_words_are_not_withheld(peer):
    async def scenario():
        peer.allow_connect.clear()
        observer = QwenStreamingPlayedAudioTranscriber(config())
        await observer.connect()
        try:
            for _ in range(10):
                await asyncio.wait_for(observer.send(b"\x10\x10" * 1600), 0.1)
            assert observer.snapshot().text == ""
            peer.allow_connect.set()
            await until(lambda: peer.sockets and len(peer.sockets[0].audio) == 1)
            socket = peer.sockets[0]
            socket.sentence("Friday, November 29, 2020")
            await until(lambda: bool(observer.events[-1].get("sentence")))
            assert observer.drain() == []
            assert observer.snapshot().text == ""
            socket.sentence("Friday, November 29, 2024", final=True)
            await until(lambda: observer.snapshot().version == 1)
            assert observer.snapshot().text == "Friday, November 29, 2024"
            assert [f.text.strip() for f in observer.drain()] == [
                "Friday, November 29, 2024"
            ]
            await observer.flush_pending()
            assert observer.snapshot().text.endswith("29, 2024")
            assert observer.snapshot().pending is False
            assert len(peer.sockets) == 1 and socket.closed
            assert observer.appends[0]["end_ms"] == 2000
            assert socket.audio[0] == b"\x10\x10" * 16000
            request = peer.requests[0]
            assert request["payload"]["model"] == DEFAULT_QWEN_ASR_STREAMING_MODEL
            assert request["payload"]["parameters"]["sample_rate"] == 8000
            assert "vocabulary" not in request["payload"]["parameters"]
        finally:
            await observer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("rate", [8000, 16000, 24000])
def test_native_asr_sends_original_pcm_bytes_and_declares_the_actual_rate(peer, rate):
    async def scenario():
        observer = QwenStreamingPlayedAudioTranscriber(config())
        await observer.connect()
        try:
            pcm = b"\x11\x12\xf0\x7a" * rate
            await observer.send_native(pcm, rate)
            await until(lambda: peer.sockets and peer.sockets[0].audio)
            assert peer.sockets[0].audio == [pcm]
            assert peer.requests[0]["payload"]["parameters"]["sample_rate"] == rate
            assert observer.appends[0]["end_ms"] == 2000
            assert observer.appends[0]["pcm_bytes"] == len(pcm)
            await observer.flush_pending()
            assert observer.summary()["input_sample_rate"] == rate
            assert observer.summary()["audio_processing"] == "native_pcm_no_resampling"
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_native_recheck_slices_the_buffer_using_its_actual_sample_rate(peer):
    async def scenario():
        peer.final_text = "Book at noon"
        observer = QwenStreamingPlayedAudioTranscriber(config())
        captured = []

        async def review(pcm, *, purpose):
            assert purpose == "recheck"
            captured.append(pcm)
            return "Book at noon"

        observer._transcribe = review
        await observer.connect()
        try:
            pcm = b"\x13\x37" * 9600  # 400 ms at 24 kHz.
            await observer.send_native(pcm, 24000)
            await observer.send_native(bytes(28800), 24000)  # 600 ms silence.
            await asyncio.wait_for(observer._segments.join(), 1)
            fragments = observer.drain()
            review = await observer.recheck(
                "Book at noon", [f.model_dump() for f in fragments]
            )
            assert review["status"] == "ok"
            assert captured == [pcm]
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_queued_sessions_freeze_stage_words_and_recheck_uses_original_stage(
    peer, monkeypatch
):
    from pathlib import Path

    from tau2.data_model.asr import HotwordSettings
    from tau2.data_model.tasks import Task
    from tau2.user.stage_asr_hotwords import load_stage_hotword_profile
    from tau2.voice.qwen_asr import QwenASRResult

    root = Path(__file__).resolve().parents[2]
    raw = json.loads((root / "data/tau2/domains/gaia2/tasks.json").read_text())
    task = Task.model_validate(
        next(t for t in raw if t["id"] == "0007_tb52xggog13zgslxysl2jpf7k9h3wgz0")
    )
    settings = HotwordSettings(
        enabled=True, mode="gaia2_stage_gt", weight=50, max_terms=50
    )
    profile = load_stage_hotword_profile(task, settings)
    reviewed = []

    async def file_asr(*args):
        reviewed.append(args[-1])
        return QwenASRResult(text="confirmed")

    monkeypatch.setattr(
        "tau2.voice.audio_native.qwen_agent.qwen_streaming_transcription.transcribe_qwen",
        file_asr,
    )

    async def scenario():
        peer.final_text = "confirmed"
        peer.allow_connect.clear()
        observer = QwenStreamingPlayedAudioTranscriber(
            config(asr_hotwords=settings), hotwords=profile
        )
        await observer.connect()
        try:
            await observer.send_native(b"\x10\x10" * 9600, 24000)
            observer.activate_stage(1)
            await observer.send_native(b"\x20\x20" * 9600, 24000)
            observer.activate_stage(2)
            await observer.send_native(b"\x30\x30" * 9600, 24000)
            assert [s.hotword_stage for s in observer.sessions] == [0, 1, 2]
            peer.allow_connect.set()
            fragments = await observer.flush_pending()
            requests = [r for r in peer.requests if r["header"]["action"] == "run-task"]
            assert [r["payload"]["parameters"]["vocabulary"] for r in requests] == [
                {"Lysverket": 50},
                {"Lysverket": 50, "Kaida Rao": 50},
                {"Lysverket": 50, "Kaida Rao": 50, "Åse": 50},
            ]
            with pytest.raises(ValueError, match="backwards"):
                observer.activate_stage(1)
            review = await observer.recheck("confirmed", [fragments[0].model_dump()])
            assert review["status"] == "ok"
            assert reviewed == [{"Lysverket": 50}]
            summary = observer.summary()
            assert [s["hotword_stage"] for s in summary["sessions"]] == [0, 1, 2]
            assert [s["stage"] for s in summary["hotword_activations"]] == [0, 1, 2]
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_short_tail_is_finished_and_idle_silence_does_not_open_sessions(peer):
    async def scenario():
        peer.final_text = "Friday, November 29, 2024"
        observer = QwenStreamingPlayedAudioTranscriber(config())
        await observer.connect()
        try:
            await observer.send(bytes(3200))
            assert peer.sockets == []
            await observer.send(b"\x10\x10" * 3200)
            await observer.send(bytes(9600))
            await asyncio.wait_for(observer._segments.join(), 1)
            first = observer.drain()
            assert first[0].start_ms == 200 and first[0].end_ms == 600
            assert first[0].text.strip().endswith("29, 2024")
            await observer.send(bytes(32000))
            assert len(peer.sockets) == 1 and peer.sockets[0].closed
            await observer.send(b"\x10\x10" * 3200)
            second = await observer.flush_pending()
            assert len(peer.sockets) == 2
            assert second[0].start_ms == 3200
            assert second[0].event_id != first[0].event_id
            assert observer.snapshot().text.count("2024") == 2
            assert all(s.status == "completed" for s in observer.sessions)
            assert [r["header"]["action"] for r in peer.requests] == [
                "run-task",
                "finish-task",
                "run-task",
                "finish-task",
            ]
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_error_does_not_flush_interim_or_expose_provider_error_body(peer):
    async def scenario():
        observer = QwenStreamingPlayedAudioTranscriber(config())
        await observer.connect()
        try:
            await observer.send(b"\x10\x10" * 8000)
            await until(lambda: peer.requests)
            socket = peer.sockets[0]
            socket.sentence("unconfirmed words")
            socket.incoming.put_nowait(
                {
                    "header": {
                        "task_id": socket.task_id,
                        "event": "task-failed",
                        "error_message": "observer-private-key",
                    },
                    "payload": {},
                }
            )
            await until(lambda: observer.failure is not None)
            with pytest.raises(RuntimeError):
                await observer.flush_pending()
            assert "observer-private-key" not in str(observer.failure)
            assert "observer-private-key" not in json.dumps(observer.summary())
            assert observer._confirmed == ""
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_finished_task_with_unresolved_text_fails_instead_of_publishing_it(peer):
    async def scenario():
        observer = QwenStreamingPlayedAudioTranscriber(config())
        await observer.connect()
        try:
            await observer.send(b"\x10\x10" * 16000)
            await until(lambda: peer.sockets and peer.sockets[0].audio)
            peer.sockets[0].sentence("still incomplete")
            await until(lambda: observer.events[-1].get("sentence"))
            with pytest.raises(RuntimeError):
                await observer.flush_pending()
            assert observer._confirmed == ""
        finally:
            await observer.close()

    asyncio.run(scenario())


def test_cumulative_tick_views_are_not_concatenated_in_caller_history():
    state = StreamingState()
    texts = ["Book Friday", "Book Friday", "Book Friday at noon", "Book Friday at noon"]
    fragments = ["Book Friday ", "", "at noon ", ""]
    for i, (text, delta) in enumerate(zip(texts, fragments)):
        live = LiveTranscriptTick(
            session_id="one-call",
            snapshot=LiveTranscriptSnapshot(version=1 if i < 2 else 2, text=text),
            fragments=[{"speaker": "agent", "text": delta}] if delta else [],
        )
        state.ticks.append(
            ParticipantTick(
                tick_id=i,
                timestamp="2026-11-21T00:00:00",
                other_chunk=AssistantMessage(
                    role="assistant",
                    content=text,
                    contains_speech=True,
                    raw_data={"live_transcript": live.model_dump()},
                ),
            )
        )
    messages = state.get_linearized_messages()
    text = " ".join(m.content for m in messages if m.content)
    assert text.count("Book Friday") == 1
    assert text.count("at noon") == 1
    assert " ".join(text.split()) == "Book Friday at noon"
    assert [t.other_chunk.content for t in state.ticks] == texts


@pytest.mark.parametrize(
    "base",
    [
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
    ],
)
def test_streaming_endpoint_and_model_selection(base):
    assert (
        qwen_streaming_asr_url(base)
        == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    )
    assert config().asr_append_seconds == 2
    with pytest.raises(ValueError):
        config(asr_append_seconds=4)

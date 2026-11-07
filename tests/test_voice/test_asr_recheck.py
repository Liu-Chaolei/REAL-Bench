"""Audio reviews use snapshot-bounded playback, without blocking normal ticks."""

import asyncio
import base64
import io
import json
import wave

import httpx

from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
from tau2.voice.audio_native.qwen_agent.mimo_transcription import (
    MiMoPlayedAudioTranscriber,
)


def test_review_cannot_read_future_audio_or_replace_original_transcript(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "fake")

    async def scenario():
        requests = []

        async def remote(request):
            body = json.loads(request.content)
            data = body["messages"][0]["content"][0]["input_audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(data))) as wav:
                requests.append((wav.getnframes(), wav.readframes(wav.getnframes())))
            text = (
                "I'll tell you tomorrow"
                if len(requests) <= 2
                else "I'll tell you the moment it is done"
            )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": text}}]}
            )

        config = QwenAgentConfig(
            asr_provider="mimo", asr_api_key_env="MIMO_API_KEY", asr_segment_seconds=1
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = MiMoPlayedAudioTranscriber(config, client=client)
            await observer.connect()
            try:
                await observer.send(b"\x10\x10" * 8000)
                await observer._segments.join()
                snapshot = observer.drain()
                await observer.send(b"\x20\x20" * 8000)
                await observer._segments.join()
                later_snapshot = observer.drain()
                result = await observer.recheck(
                    "tomorrow", [f.model_dump() for f in snapshot]
                )
                assert result["status"] == "ok" and result["end_ms"] == 1000
                assert result["review_text"] == "I'll tell you the moment it is done"
                assert abs(requests[-1][0] / 24000 - 1) < 0.001
                assert requests[-1][1] == b"\x10\x10" * requests[-1][0]
                assert (
                    observer._fragment_ranges[snapshot[0].event_id]["text"]
                    == "I'll tell you tomorrow"
                )
                assert observer.drain() == []  # review is never injected as primary ASR
                again = await observer.recheck(
                    "tomorrow", [f.model_dump() for f in snapshot]
                )
                assert again["cached"] and len(requests) == 3
                latest = await observer.recheck(
                    "tomorrow", [f.model_dump() for f in snapshot + later_snapshot]
                )
                assert latest["fragment_ids"] == [later_snapshot[0].event_id]
                assert latest["end_ms"] == 2000 and len(requests) == 4
                unknown = await observer.recheck(
                    "tomorrow", [{"event_id": "made-up", "end_ms": 999999}]
                )
                assert unknown["status"] == "unavailable" and len(requests) == 4
            finally:
                await observer.close()

    asyncio.run(scenario())


def test_review_timeout_keeps_media_and_primary_asr_healthy(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "fake")

    async def scenario():
        entered = asyncio.Event()
        calls = []

        async def remote(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": "tomorrow"}}]}
                )
            entered.set()
            await asyncio.Event().wait()

        config = QwenAgentConfig(
            asr_provider="mimo",
            asr_api_key_env="MIMO_API_KEY",
            asr_segment_seconds=1,
            asr_recheck_timeout_seconds=0.05,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = MiMoPlayedAudioTranscriber(config, client=client)
            await observer.connect()
            try:
                await observer.send(b"\x10\x10" * 8000)
                await observer._segments.join()
                fragments = observer.drain()
                review = asyncio.create_task(
                    observer.recheck("tomorrow", [f.model_dump() for f in fragments])
                )
                await entered.wait()
                await asyncio.wait_for(observer.send(bytes(1600)), 0.02)
                assert not review.done()
                assert (await review)["status"] == "timeout"
                assert observer.failure is None
            finally:
                await observer.close()

    asyncio.run(scenario())


def test_expired_playback_is_not_reviewed(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "fake")

    async def scenario():
        calls = []

        def remote(request):
            calls.append(request)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "tomorrow"}}]}
            )

        config = QwenAgentConfig(
            asr_provider="mimo",
            asr_api_key_env="MIMO_API_KEY",
            asr_segment_seconds=1,
            asr_recheck_buffer_seconds=1,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = MiMoPlayedAudioTranscriber(config, client=client)
            await observer.connect()
            try:
                await observer.send(b"\x10\x10" * 8000)
                await observer._segments.join()
                snapshot = observer.drain()
                await observer.send(bytes(16000))
                await observer._segments.join()
                count = len(calls)
                result = await observer.recheck(
                    "tomorrow", [f.model_dump() for f in snapshot]
                )
                assert result["status"] == "unavailable" and len(calls) == count
            finally:
                await observer.close()

    asyncio.run(scenario())

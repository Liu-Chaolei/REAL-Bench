"""PCM format boundaries with local audio and HTTP doubles only."""

import asyncio
import base64
import io
import json
import struct
import wave
from types import SimpleNamespace

import httpx
import pytest

from tau2.adapters.qwen_audio_agent.adapter import DiscreteTimeQwenAgentAdapter
from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
from tau2.adapters.qwen_audio_agent.transcription.qwen import QwenPlayedAudioTranscriber
from tau2.agent.discrete_time_audio_native_agent import DiscreteTimeAudioNativeAgent
from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT, AudioEncoding, AudioFormat
from tau2.data_model.message import UserMessage
from tau2.voice.audio_native.tick_result import TickResult


def config():
    return QwenAgentConfig(
        asr_provider="qwen",
        asr_model="qwen-audio-3.1-asr-flash",
        asr_base_url="https://dashscope.aliyuncs.com",
        asr_api_key_env="QWEN_PCM_TEST_KEY",
        asr_segment_seconds=1,
    )


def test_reset_keeps_native_output_rate_and_audio_duration():
    adapter = DiscreteTimeQwenAgentAdapter(
        200, config(), provider=SimpleNamespace(), transcriber=SimpleNamespace()
    )
    for _ in range(2):
        adapter.clear_buffers()
        assert adapter.audio_format.sample_rate == 16000
        assert adapter.playback.format == adapter.playback_audio_format
        assert adapter._speech_detector.rate == 24000
        result = SimpleNamespace(events=[], agent_audio_chunks=[])
        adapter._process_event(
            result,
            {
                "type": "audio",
                "response_id": "reply",
                "sample_rate": 24000,
                "audio": base64.b64encode(bytes(9600)).decode(),
            },
        )
        audio, identifier = result.agent_audio_chunks[0]
        assert audio == bytes(9600)
        adapter.playback.append(audio, identifier)
        row = adapter.playback.queue[-1]
        assert row["end"] - row["start"] == pytest.approx(0.2)


@pytest.mark.parametrize("rate", [16000, 24000])
def test_provider_input_uses_negotiated_pcm_rate(rate):
    async def scenario():
        async def noop(*args):
            pass

        adapter = DiscreteTimeQwenAgentAdapter(
            200,
            config(),
            provider=SimpleNamespace(input_sample_rate=rate, connect=noop),
            transcriber=SimpleNamespace(validate_config=lambda: None, connect=noop),
        )
        await adapter._connect("policy", [])
        # A nonzero constant catches accidental mu-law decoding as well as rate errors.
        audio = struct.pack("<h", 1000) * 3200
        converted = adapter._input_converter.convert_input(audio)
        assert abs(len(converted) // 2 - rate * 0.2) <= 1
        assert set(struct.unpack(f"<{len(converted) // 2}h", converted)) == {1000}

    asyncio.run(scenario())


def test_native_silence_is_200ms_and_does_not_fill_asr_queue(monkeypatch):
    monkeypatch.setenv("QWEN_PCM_TEST_KEY", "fake-key")

    async def scenario():
        observer = QwenPlayedAudioTranscriber(config())
        adapter = DiscreteTimeQwenAgentAdapter(
            200,
            config(),
            provider=SimpleNamespace(session_id="offline"),
            transcriber=observer,
        )
        result = TickResult(
            tick_number=1,
            audio_sent_bytes=6400,
            audio_sent_duration_ms=200,
            played_audio=bytes(9600),
            playback_audio_format=adapter.playback_audio_format,
        )
        for _ in range(40):
            await adapter._after_playback(result)
        assert observer.sent_ms == 8000
        assert observer.played_sample_rate == 24000
        assert observer._segments.empty()
        assert not observer._pending

    asyncio.run(scenario())


def test_http_asr_resamples_directly_from_pcm24_to_pcm16(monkeypatch):
    monkeypatch.setenv("QWEN_PCM_TEST_KEY", "fake-key")

    async def scenario():
        async def remote(request):
            body = json.loads(request.content)
            encoded = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
            with wave.open(
                io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))
            ) as wav:
                assert wav.getframerate() == 16000
                assert wav.getnframes() == 16000
                assert wav.getsampwidth() == 2
            return httpx.Response(200, json={"output": {"text": "heard"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = QwenPlayedAudioTranscriber(config(), client=client)
            await observer.connect()
            try:
                await observer.send_native(struct.pack("<h", 1000) * 24000, 24000)
                await observer._segments.join()
                assert observer.sent_ms == 1000
                assert observer.drain()[0].text == "heard "
            finally:
                await observer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fmt",
    [
        TELEPHONY_AUDIO_FORMAT,
        AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=24000),
    ],
)
def test_agent_rejects_wrong_external_format(fmt):
    agent = DiscreteTimeAudioNativeAgent(
        tools=[],
        domain_policy="policy",
        provider="qwen_agent",
        qwen_agent_config=config(),
    )
    with pytest.raises(ValueError, match="Expected Agent input"):
        agent._extract_user_audio(
            UserMessage(
                role="user", is_audio=True, audio_content="AAA=", audio_format=fmt
            )
        )

"""MiMo contracts: real audio payloads, delayed observation, private settings."""

import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from tenacity import stop_after_attempt, wait_none

from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
from tau2.adapters.qwen_audio_agent.transcription.mimo import (
    MiMoPlayedAudioTranscriber,
)
from tau2.data_model.voice import MiMoTTSConfig, SynthesisConfig
from tau2.voice.mimo import pcm_wav, synthesize_mimo
from tau2.voice.synthesis.synthesize import synthesize_voice


def test_mimo_tts_normalizes_wav_and_preserves_provider_on_reload(
    monkeypatch, tmp_path
):
    secret = tmp_path / "voice.env"
    secret.write_text("MIMO_API_KEY=synthetic-secret\n")
    captured = []

    def reply(request):
        captured.append(json.loads(request.content))
        assert request.headers["api-key"] == "synthetic-secret"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "audio": {
                                "data": base64.b64encode(
                                    pcm_wav(b"\x10\x10" * 2400, 24000)
                                ).decode(),
                            }
                        }
                    }
                ]
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        "tau2.voice.mimo.httpx.Client",
        lambda **kw: client_type(transport=httpx.MockTransport(reply), **kw),
    )
    config = MiMoTTSConfig(config_env_file=str(secret), voice_id="Chloe")
    settings = SynthesisConfig(provider="mimo", provider_config=config)
    reloaded = SynthesisConfig.model_validate_json(settings.model_dump_json())
    assert isinstance(reloaded.provider_config, MiMoTTSConfig)
    audio = synthesize_mimo("Read order 123.", reloaded.provider_config)
    assert audio.format.sample_rate == 16000
    assert audio.format.is_pcm16
    assert abs(audio.duration - 0.1) < 0.001
    assert captured[0]["messages"] == [
        {"role": "assistant", "content": "Read order 123."}
    ]
    assert captured[0]["audio"] == {"format": "wav", "voice": "Chloe"}
    assert "synthetic-secret" not in settings.model_dump_json()


def test_mimo_observer_does_not_block_playback_or_disclose_unfinished_recognition(
    monkeypatch,
):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def reply(request):
            body = json.loads(request.content)
            payload = body["messages"][0]["content"][0]["input_audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(payload))) as wav:
                assert wav.getframerate() == 24000
                assert abs(wav.getnframes() / wav.getframerate() - 0.6) < 0.001
            calls.append(body)
            started.set()
            await release.wait()
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "It is shipped."}}]}
            )

        monkeypatch.setenv("MIMO_API_KEY", "synthetic-secret")
        config = QwenAgentConfig(
            asr_provider="mimo",
            asr_model="mimo-v2.5-asr",
            asr_api_key_env="MIMO_API_KEY",
            asr_silence_seconds=0.2,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            observer = MiMoPlayedAudioTranscriber(config, client=client)
            await observer.connect()
            try:
                await observer.send(
                    bytes(3200)
                )  # 200 ms silence, never recognized alone.
                assert observer.drain() == []
                await observer.send(b"\x10\x10" * 3200)  # 400 ms actual playback.
                await observer.send(bytes(3200))
                await asyncio.wait_for(started.wait(), 1)
                assert observer.drain() == []
                await asyncio.wait_for(observer.send(bytes(3200)), 0.1)
                release.set()
                await asyncio.wait_for(observer._segments.join(), 1)
                (fragment,) = observer.drain()
                assert fragment.text.strip() == "It is shipped."
                assert (fragment.start_ms, fragment.end_ms) == (200, 600)
                assert fragment.end_ms <= observer.sent_ms
                assert len(calls) == 1
                assert observer.summary()["word_timestamps"] is False
            finally:
                await observer.close()
            await observer.connect()
            try:
                assert observer.sent_ms == 0
                assert observer.drain() == []
                assert observer.summary()["finalized_segments"] == 0
            finally:
                await observer.close()

    asyncio.run(scenario())


def test_mimo_error_body_is_not_copied_to_diagnostics(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "synthetic-secret")
    client_type = httpx.Client
    monkeypatch.setattr(
        "tau2.voice.mimo.httpx.Client",
        lambda **kw: client_type(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    401,
                    json={
                        "error": {"message": "echo synthetic-secret"},
                    },
                )
            ),
            **kw,
        ),
    )
    with pytest.raises(RuntimeError, match="HTTP 401") as error:
        synthesize_mimo("Hello", MiMoTTSConfig())
    assert "synthetic-secret" not in str(error.value)


def test_mimo_tts_connection_error_retries(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", "synthetic-secret")
    calls = []

    def reply(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "audio": {
                                "data": base64.b64encode(
                                    pcm_wav(b"\x10\x10" * 2400, 16000)
                                ).decode(),
                            }
                        }
                    }
                ]
            },
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        "tau2.voice.mimo.httpx.Client",
        lambda **kw: client_type(transport=httpx.MockTransport(reply), **kw),
    )

    retrying_synthesize = synthesize_voice.retry_with(
        stop=stop_after_attempt(3), wait=wait_none()
    )
    audio = retrying_synthesize("Hello", "mimo", MiMoTTSConfig())

    assert audio.format.is_pcm16
    assert len(calls) == 2


@pytest.mark.parametrize(
    "model, roles",
    [
        ("openai/responses/MiniMax-M3", ["system", "user"]),
        ("gpt-4.1", ["system", "system"]),
    ],
)
def test_minimax_initial_customer_request_keeps_prompt_text_and_has_conversational_input(
    monkeypatch,
    model,
    roles,
):
    from litellm import ModelResponse

    from tau2.data_model.message import SystemMessage
    from tau2.utils import llm_utils

    captured = []

    def complete(**kwargs):
        captured.append(kwargs)
        return ModelResponse(
            model="MiniMax-M3",
            choices=[
                {
                    "message": {"role": "assistant", "content": "Hello."},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )

    monkeypatch.setattr(llm_utils, "completion", complete)
    monkeypatch.setattr(llm_utils, "get_response_cost", lambda response: 0)
    messages = [
        SystemMessage(role="system", content="Customer scenario and constraints."),
        SystemMessage(role="system", content="Respond as the customer."),
    ]
    result = llm_utils.generate(
        model,
        messages,
        call_name="user_streaming_response",
        num_retries=0,
    )
    assert result.content == "Hello."
    assert [m["role"] for m in captured[0]["messages"]] == roles
    assert [m["content"] for m in captured[0]["messages"]] == [
        m.content for m in messages
    ]
    assert [m.role for m in messages] == ["system", "system"]


def test_listener_decision_model_uses_configured_service_without_changing_policy(
    monkeypatch,
):
    from types import SimpleNamespace

    from tau2.data_model.message import AssistantMessage
    from tau2.user import user_simulator_streaming as module

    messages = [AssistantMessage(role="assistant", content="Let me check your order.")]
    state = SimpleNamespace(
        get_linearized_messages=lambda **kwargs: messages,
        input_turn_taking_buffer=messages,
    )
    captured = []

    def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content="NO", generation_time_seconds=0.1, cost=None, usage=None
        )

    monkeypatch.setattr(module, "generate", generate)
    decision = module.user_interruption_policy(
        state,
        decision_model="openai/responses/MiniMax-M3",
        decision_model_args={"reasoning_effort": "low", "num_retries": 0},
    )
    assert decision.decision is False
    assert captured[0]["model"] == "openai/responses/MiniMax-M3"
    assert captured[0]["num_retries"] == 0

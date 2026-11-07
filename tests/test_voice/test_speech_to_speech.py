"""Contracts for the native HF frontend's MiMo audio translation boundary."""

import asyncio
import base64
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tau2.voice.audio_native.qwen_agent.config import (
    QwenAgentConfig,
    SpeechToSpeechConfig,
)

ROOT = Path(__file__).resolve().parents[2] / "integrations/qwen_audio_agent/speech"
spec = importlib.util.spec_from_file_location("mimo_gateway", ROOT / "mimo_gateway.py")
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)
spec = importlib.util.spec_from_file_location("llm_fallback", ROOT / "llm_fallback.py")
fallback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fallback)


def wav(rate=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x20\x01" * rate)
    return stream.getvalue()


@pytest.fixture
def audio_service():
    pytest.importorskip(
        "python_multipart",
        reason="Requires the standalone speech runtime's multipart dependency",
    )
    requests = []

    def remote(request):
        import json

        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["api-key"] == "mimo-secret"
        message = (
            {"content": "Read order 123."}
            if body["model"].endswith("asr")
            else {"audio": {"data": base64.b64encode(wav(24000)).decode()}}
        )
        return httpx.Response(200, json={"choices": [{"message": message}]})

    settings = SpeechToSpeechConfig().model_dump()
    app = gateway.create_app(
        settings, "mimo-secret", "local-token", httpx.MockTransport(remote)
    )
    with TestClient(app, headers={"Authorization": "Bearer local-token"}) as client:
        yield client, requests


def test_native_asr_upload_uses_mimo_and_normalizes_rate(audio_service):
    client, requests = audio_service
    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("speech.wav", wav(), "audio/wav")},
        data={"model": "mimo-v2.5-asr", "response_format": "json", "language": "en"},
    )
    assert response.json() == {"text": "Read order 123.", "language": "en"}
    audio = requests[0]["messages"][0]["content"][0]["input_audio"]["data"]
    with wave.open(io.BytesIO(base64.b64decode(audio))) as reader:
        assert reader.getframerate() == 24000
    assert requests[0]["asr_options"] == {"language": "en"}


def test_native_tts_speaks_exact_text_and_returns_headerless_pcm(audio_service):
    client, requests = audio_service
    text = "Your order is shipped."
    response = client.post(
        "/v1/audio/speech",
        json={
            "model": "mimo-v2.5-tts",
            "input": text,
            "voice": "Dean",
            "response_format": "pcm",
            "stream_format": "audio",
        },
    )
    assert response.status_code == 200
    assert response.content == b"\x20\x01" * 24000
    assert requests[0]["messages"] == [{"role": "assistant", "content": text}]
    assert requests[0]["stream"] is False
    assert requests[0]["audio"] == {"format": "wav", "voice": "Dean"}


def test_audio_rejects_unsupported_settings_and_credentials(audio_service):
    client, requests = audio_service
    body = {
        "model": "mimo-v2.5-tts",
        "input": "Hello",
        "voice": "Dean",
        "response_format": "pcm",
    }
    assert client.post("/v1/audio/speech", json={**body, "speed": 2}).status_code == 422
    assert (
        client.post(
            "/v1/audio/speech", json=body, headers={"Authorization": "bad"}
        ).status_code
        == 403
    )
    assert requests == []
    with pytest.raises(HTTPException):
        gateway.wav_pcm(b"not a wav")


def test_provider_errors_are_sanitized():
    def reject(request):
        return httpx.Response(401, text="mimo-secret and private provider response")

    app = gateway.create_app(
        SpeechToSpeechConfig().model_dump(),
        "mimo-secret",
        "token",
        httpx.MockTransport(reject),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            headers={"Authorization": "Bearer token"},
            json={
                "model": "mimo-v2.5-tts",
                "input": "Hello",
                "voice": "Dean",
                "response_format": "pcm",
            },
        )
    assert response.status_code == 502
    assert "401" in response.text and "secret" not in response.text


def test_disconnect_cancels_pending_provider_request():
    async def scenario():
        cancelled = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        request = AsyncMock()
        request.receive.return_value = {"type": "http.disconnect"}
        with pytest.raises(HTTPException) as error:
            await gateway.until_disconnected(request, operation())
        assert error.value.status_code == 499
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_speech_credentials_are_separate_and_stale_realtime_is_not_resolved(tmp_path):
    path = tmp_path / "config.env"
    original = """LLM_API_KEY=frontend-secret
LLM_API_URL=https://frontend.invalid/v1
LLM_MODEL=gpt-6.1-sol
MIMO_API_KEY=mimo-secret
GAIA2_BACKEND_API_KEY=backend-secret
GAIA2_BACKEND_API_BASE=https://backend.invalid/v1
GAIA2_BACKEND_MODEL=MiniMax-M3
QWEN_AUDIO_REALTIME_API_KEY=${MISSING_INACTIVE}
QWEN_AUDIO_REALTIME_PROVIDER=dashscope
QWEN_AUDIO_REALTIME_MODEL=old-realtime-model
UNRELATED_JUDGE_KEY=judge-secret
"""
    path.write_text(original)
    config = QwenAgentConfig(
        config_env_file=str(path), speech_to_speech=SpeechToSpeechConfig()
    )
    env = config.resolved_environment({})
    assert env["QWEN_AUDIO_REALTIME_PROVIDER"] == "speech-to-speech"
    assert env["QWEN_AUDIO_REALTIME_API_KEY"] == ""
    assert env["QWEN_AUDIO_REALTIME_MODEL"] == env["QWEN_AUDIO_REALTIME_VOICE"] == ""
    assert env["TAU_S2S_LLM_API_KEY"] == "frontend-secret"
    assert env["OPENAI_API_KEY"] == "backend-secret"
    assert env["OPENAI_MODEL"] == "MiniMax-M3"
    assert "UNRELATED_JUDGE_KEY" not in env
    assert "secret" not in config.model_dump_json()
    assert path.read_text() == original
    with pytest.raises(ValueError, match="environment variable names"):
        SpeechToSpeechConfig(llm_api_key_env="literal-key")


def test_mimo_caller_and_frontend_use_same_refs_without_changing_backend(tmp_path):
    path = tmp_path / "config.env"
    path.write_text(
        "MIMO_API_KEY=caller-secret\nMIMO_BASE_URL=https://mimo.invalid/v1\n"
        "MIMO_MODEL=mimo-v2.6-pro\nGAIA2_BACKEND_API_KEY=backend-secret\n"
        "GAIA2_BACKEND_API_BASE=https://backend.invalid/v1\n"
        "GAIA2_BACKEND_MODEL=MiniMax-M3\n"
    )
    config = QwenAgentConfig(
        config_env_file=str(path),
        user_llm_api_key_env="MIMO_API_KEY",
        user_llm_base_url_env="MIMO_BASE_URL",
        user_llm_model_env="MIMO_MODEL",
        speech_to_speech=SpeechToSpeechConfig(
            llm_api_key_env="MIMO_API_KEY",
            llm_base_url_env="MIMO_BASE_URL",
            llm_model_env="MIMO_MODEL",
        ),
    )
    model, args = config.user_llm_settings()
    assert model == "openai/mimo-v2.6-pro"
    assert args["api_base"] == "https://mimo.invalid/v1"
    env = config.resolved_environment({})
    assert env["TAU_S2S_LLM_MODEL"] == "mimo-v2.6-pro"
    assert env["TAU_S2S_LLM_API_KEY"] == args["api_key"]
    assert env["OPENAI_MODEL"] == "MiniMax-M3"
    assert "caller-secret" not in config.model_dump_json()
    assert QwenAgentConfig().user_llm_settings() is None


def sse(*events):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join("data: " + json.dumps(event) + "\n\n" for event in events),
    )


def success():
    return sse(
        {"type": "response.created", "response": {"id": "backup-or-healthy"}},
        {"type": "response.output_text.delta", "delta": "Ready."},
        {"type": "response.completed", "response": {"status": "completed"}},
    )


def llm_app(remote):
    app = gateway.create_app(
        SpeechToSpeechConfig().model_dump(),
        "mimo-secret",
        "local-token",
        httpx.MockTransport(remote),
    )
    fallback.install_llm_fallback(
        app,
        "local-token",
        [
            ("https://primary.invalid/v1", "primary-secret"),
            ("https://secondary.invalid/v1", "secondary-secret"),
        ],
    )
    return app


PAYLOAD = {
    "model": "MiniMax-M3",
    "input": [{"role": "user", "content": "Read order 123."}],
    "reasoning": {"effort": "low"},
    "tools": [{"type": "function", "name": "read_order", "parameters": {}}],
    "stream": True,
}


def test_healthy_primary_never_uses_backup_and_preserves_request():
    requests = []

    def remote(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer primary-secret"
        assert json.loads(request.content) == PAYLOAD
        return success()

    with TestClient(llm_app(remote)) as client:
        response = client.post(
            "/v1/responses",
            json=PAYLOAD,
            headers={"Authorization": "Bearer local-token"},
        )
    assert "Ready." in response.text
    assert len(requests) == 1
    assert requests[0].url.host == "primary.invalid"


@pytest.mark.parametrize(
    "failure", [401, 403, 429, 503, "timeout", "quota", "base_resp", "truncated"]
)
def test_frontend_falls_back_and_stays_on_secondary(failure, caplog):
    requests = []

    def remote(request):
        requests.append(request)
        assert json.loads(request.content) == PAYLOAD
        if request.url.host == "secondary.invalid":
            assert request.headers["authorization"] == "Bearer secondary-secret"
            return success()
        assert request.headers["authorization"] == "Bearer primary-secret"
        if failure == "timeout":
            raise httpx.ReadTimeout(
                "primary-secret https://primary.invalid", request=request
            )
        if failure == "quota":
            return sse(
                {"type": "response.created", "response": {"id": "failed-primary"}},
                {
                    "type": "response.failed",
                    "response": {
                        "error": {
                            "code": "insufficient_quota",
                            "message": "plan quota exhausted (2056)",
                        }
                    },
                },
            )
        if failure == "base_resp":
            return httpx.Response(200, json={"base_resp": {"status_code": 2062}})
        if failure == "truncated":
            return sse(
                {"type": "response.created", "response": {"id": "failed-primary"}}
            )
        return httpx.Response(failure, text="private provider error primary-secret")

    with TestClient(llm_app(remote)) as client:
        for _ in range(2):
            response = client.post(
                "/v1/responses",
                json=PAYLOAD,
                headers={"Authorization": "Bearer local-token"},
            )
            assert "Ready." in response.text
            assert "failed-primary" not in response.text
            assert "secret" not in response.text
    assert [r.url.host for r in requests] == [
        "primary.invalid",
        "secondary.invalid",
        "secondary.invalid",
    ]
    assert "fallback activated" in caplog.text
    assert "secret" not in caplog.text


def test_partial_tool_output_is_not_replayed_but_next_request_uses_backup():
    requests = []

    def remote(request):
        requests.append(request.url.host)
        if request.url.host == "secondary.invalid":
            return success()
        return sse(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "call_id": "call-once",
                    "name": "read_order",
                },
            },
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error"}},
            },
        )

    with TestClient(llm_app(remote)) as client:
        headers = {"Authorization": "Bearer local-token"}
        response = client.post("/v1/responses", json=PAYLOAD, headers=headers)
        assert "call-once" in response.text
        assert "frontend_llm_unavailable" in response.text
        assert requests == ["primary.invalid"]
        response = client.post("/v1/responses", json=PAYLOAD, headers=headers)
        assert "Ready." in response.text
        assert requests == ["primary.invalid", "secondary.invalid"]


def test_both_endpoints_fail_without_exposing_provider_credentials():
    requests = []

    def remote(request):
        requests.append(request.url.host)
        return httpx.Response(401, text="primary-secret secondary-secret private-error")

    with TestClient(llm_app(remote)) as client:
        response = client.post(
            "/v1/responses",
            json=PAYLOAD,
            headers={"Authorization": "Bearer local-token"},
        )
        unauthorized = client.post("/v1/responses", json=PAYLOAD)
    assert requests == ["primary.invalid", "secondary.invalid"]
    assert "frontend_llm_unavailable" in response.text
    assert "secret" not in response.text and "private-error" not in response.text
    assert unauthorized.status_code == 403


def test_backup_credentials_resolve_as_a_pair_without_modifying_private_file(tmp_path):
    path = tmp_path / "config.env"
    original = """MINIMAX_API_KEY=primary-secret
MINIMAX_BASE_URL=https://primary.invalid/v1
MINIMAX_API_KEY2=secondary-secret
MINIMAX_BASE_URL2=https://secondary.invalid/v1
MINIMAX_MODEL=MiniMax-M3
MIMO_API_KEY=mimo-secret
GAIA2_BACKEND_API_KEY=backend-secret
GAIA2_BACKEND_API_BASE=https://backend.invalid/v1
GAIA2_BACKEND_MODEL=backend-model
"""
    path.write_text(original)
    speech = SpeechToSpeechConfig(
        llm_api_key_env="MINIMAX_API_KEY",
        llm_base_url_env="MINIMAX_BASE_URL",
        llm_model_env="MINIMAX_MODEL",
        llm_fallback_api_key_env="MINIMAX_API_KEY2",
        llm_fallback_base_url_env="MINIMAX_BASE_URL2",
    )
    config = QwenAgentConfig(config_env_file=str(path), speech_to_speech=speech)
    env = config.resolved_environment({})
    assert env["TAU_S2S_LLM_FALLBACK_API_KEY"] == "secondary-secret"
    assert env["TAU_S2S_LLM_FALLBACK_BASE_URL"] == "https://secondary.invalid/v1"
    assert env["OPENAI_API_KEY"] == "backend-secret"
    assert "secret" not in config.model_dump_json()
    assert path.read_text() == original
    with pytest.raises(ValueError, match="both key and base URL"):
        SpeechToSpeechConfig(llm_fallback_api_key_env="MINIMAX_API_KEY2")
    with pytest.raises(ValueError, match="environment variable names"):
        SpeechToSpeechConfig(
            llm_fallback_api_key_env="literal-key",
            llm_fallback_base_url_env="MINIMAX_BASE_URL2",
        )


def test_cancellation_does_not_activate_fallback():
    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()
        requests = []

        async def remote(request):
            requests.append(request.url.host)
            if len(requests) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return success()

        app = llm_app(remote)
        route = next(r for r in app.routes if r.path == "/v1/responses")
        request = AsyncMock()
        request.headers = {"authorization": "Bearer local-token"}
        request.json.return_value = PAYLOAD
        async with app.router.lifespan_context(app):
            response = await route.endpoint(request)
            task = asyncio.create_task(anext(response.body_iterator))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
            response = await route.endpoint(request)
            output = [block async for block in response.body_iterator]
            assert any("Ready." in block for block in output)
        assert requests == ["primary.invalid", "primary.invalid"]

    asyncio.run(scenario())


def test_native_pipeline_uses_local_proxy_only_when_fallback_is_configured(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("speech_server", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    env = {
        "TAU_S2S_AUDIO_TOKEN": "local-token",
        "TAU_S2S_LLM_API_KEY": "primary-secret",
        "TAU_S2S_LLM_BASE_URL": "https://primary.invalid/v1",
        "TAU_S2S_LLM_MODEL": "MiniMax-M3",
    }
    settings = SpeechToSpeechConfig().model_dump()
    direct = server.pipeline_document(settings, env)["blocks"]["llm"]["settings"]
    assert direct["responses_api_base_url"] == env["TAU_S2S_LLM_BASE_URL"]
    env["TAU_S2S_LLM_FALLBACK_API_KEY"] = "secondary-secret"
    proxied = server.pipeline_document(settings, env)["blocks"]["llm"]["settings"]
    assert proxied["responses_api_base_url"] == "http://127.0.0.1:8766/v1"
    assert proxied["responses_api_api_key"] == "local-token"
    assert proxied["responses_api_reasoning_effort"] == "low"
    assert "secret" not in json.dumps(proxied)


def test_old_image_cannot_silently_ignore_requested_fallback(monkeypatch):
    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(
        QwenAgentConfig(
            speech_to_speech=SpeechToSpeechConfig(
                llm_fallback_api_key_env="MINIMAX_API_KEY2",
                llm_fallback_base_url_env="MINIMAX_BASE_URL2",
            )
        )
    )
    commands = []

    def command(*args, **kwargs):
        commands.append(args)
        return json.dumps(
            [
                {
                    "Config": {
                        "Labels": {
                            "org.tau.qwen-agent.protocol": "1",
                            "org.tau.qwen-agent.confinement": "mount-namespace-v1",
                            "org.tau.speech-to-speech": "1.1.0",
                        }
                    }
                }
            ]
        )

    monkeypatch.setattr(runtime, "_command", command)
    with pytest.raises(ValueError, match="frontend LLM fallback image"):
        runtime.start()
    assert len(commands) == 1
    assert runtime.container_id is None


@pytest.mark.parametrize("stream_field", [{}, {"stream": False}])
def test_nonstream_warmup_and_compaction_use_the_same_fallback(stream_field):
    requests = []
    payload = {key: value for key, value in PAYLOAD.items() if key != "stream"}
    payload.update(stream_field)

    def remote(request):
        requests.append(request.url.host)
        assert json.loads(request.content) == payload
        if request.url.host == "primary.invalid":
            return httpx.Response(
                200,
                json={
                    "error": {
                        "code": "insufficient_quota",
                        "message": "plan quota exhausted (2056)",
                    }
                },
            )
        return httpx.Response(
            200, json={"id": "backup", "status": "completed", "output": []}
        )

    with TestClient(llm_app(remote)) as client:
        for _ in range(2):
            response = client.post(
                "/v1/responses",
                json=payload,
                headers={"Authorization": "Bearer local-token"},
            )
            assert response.json()["id"] == "backup"
    assert requests == ["primary.invalid", "secondary.invalid", "secondary.invalid"]

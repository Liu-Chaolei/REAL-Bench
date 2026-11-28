"""Qwen frontend ASR routing, credential separation and image compatibility."""

import base64
import importlib.util
import io
import json
import sys
import wave
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tau2.adapters.qwen_audio_agent.config import (
    QwenAgentConfig,
    SpeechToSpeechConfig,
)

ROOT = Path(__file__).resolve().parents[2] / "integrations/qwen_audio_agent/speech"
spec = importlib.util.spec_from_file_location(
    "qwen_frontend_audio_gateway", ROOT / "mimo_gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)


def wav(rate):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x20\x01" * rate)
    return stream.getvalue()


@pytest.mark.parametrize(
    "base",
    [
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/",
        "https://dashscope.aliyuncs.com/api/v1",
        "https://dashscope.aliyuncs.com",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    ],
)
def test_realtime_and_http_bases_select_the_documented_asr_endpoint(base):
    assert gateway.dashscope_asr_url(base) == (
        "https://dashscope.aliyuncs.com"
        "/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert gateway.dashscope_asr_url(
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
    ).startswith("https://dashscope-intl.aliyuncs.com/")


@pytest.mark.parametrize(
    "base",
    [
        "file:///private",
        "https://user:private@dashscope.aliyuncs.com",
        "https://dashscope.aliyuncs.com?api_key=private",
        "https://dashscope.aliyuncs.com/unrecognized",
    ],
)
def test_invalid_endpoint_errors_do_not_echo_connection_settings(base):
    with pytest.raises(ValueError, match="DashScope ASR base URL") as error:
        gateway.dashscope_asr_url(base)
    assert "private" not in str(error.value)


def test_qwen_asr_uses_its_own_endpoint_and_key_while_tts_uses_mimo():
    pytest.importorskip("python_multipart")
    requests = []

    def remote(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if body["model"] == "qwen-audio-3.1-asr-flash":
            assert str(request.url) == (
                "https://dashscope.aliyuncs.com"
                "/api/v1/services/aigc/multimodal-generation/generation"
            )
            assert request.headers["authorization"] == "Bearer qwen-private"
            assert request.headers["x-dashscope-sse"] == "disable"
            assert "api-key" not in request.headers
            data = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
            assert data.startswith("data:audio/wav;base64,")
            with wave.open(
                io.BytesIO(base64.b64decode(data.split(",", 1)[1]))
            ) as reader:
                assert reader.getframerate() == 16000
                assert reader.getnchannels() == 1 and reader.getsampwidth() == 2
                assert abs(reader.getnframes() / 16000 - 1) < 0.001
            return httpx.Response(
                200,
                json={
                    "output": {"text": "Read order 123."},
                    "usage": {"input_tokens": 50, "output_tokens": 5},
                },
            )
        assert str(request.url) == "https://api.xiaomimimo.com/v1/chat/completions"
        assert request.headers["api-key"] == "mimo-private"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "audio": {"data": base64.b64encode(wav(24000)).decode()}
                        }
                    }
                ]
            },
        )

    app = gateway.create_app(
        SpeechToSpeechConfig(asr_model="qwen-audio-3.1-asr-flash").model_dump(),
        "mimo-private",
        "local-token",
        httpx.MockTransport(remote),
        asr_key="qwen-private",
        asr_base_url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
    )
    with TestClient(app, headers={"Authorization": "Bearer local-token"}) as client:
        for language in ("en", "auto"):
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("speech.wav", wav(24000), "audio/wav")},
                data={"model": "qwen-audio-3.1-asr-flash", "language": language},
            )
            assert response.json() == {
                "text": "Read order 123.",
                "language": language,
            }
        assert requests[0][1]["parameters"] == {
            "format": "wav",
            "sample_rate": "16000",
            "language_hints": ["en"],
        }
        assert "language_hints" not in requests[1][1]["parameters"]
        response = client.post(
            "/v1/audio/speech",
            json={
                "model": "mimo-v2.5-tts",
                "input": "Hello.",
                "voice": "Dean",
                "response_format": "pcm",
            },
        )
        assert response.status_code == 200
        assert response.content == b"\x20\x01" * 24000
        unauthorized = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("speech.wav", wav(16000), "audio/wav")},
            data={"model": "qwen-audio-3.1-asr-flash"},
            headers={"Authorization": "bad"},
        )
        assert unauthorized.status_code == 403
        assert len(requests) == 3


@pytest.mark.parametrize("failure", ["http", "timeout", "invalid", "provider_error"])
def test_qwen_errors_are_sanitized(failure):
    pytest.importorskip("python_multipart")

    def remote(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("qwen-private", request=request)
        if failure == "invalid":
            return httpx.Response(200, text="qwen-private")
        if failure == "provider_error":
            return httpx.Response(200, json={"message": "qwen-private"})
        return httpx.Response(401, text="qwen-private")

    app = gateway.create_app(
        SpeechToSpeechConfig(asr_model="qwen-audio-3.1-asr-flash").model_dump(),
        "mimo-private",
        "local-token",
        httpx.MockTransport(remote),
        asr_key="qwen-private",
        asr_base_url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
    )
    with TestClient(app, headers={"Authorization": "Bearer local-token"}) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("speech.wav", wav(16000), "audio/wav")},
            data={"model": "qwen-audio-3.1-asr-flash"},
        )
    assert response.status_code == (504 if failure == "timeout" else 502)
    assert "Qwen ASR" in response.text and "private" not in response.text


def test_selected_dotenv_references_are_resolved_without_exporting_other_secrets(
    tmp_path,
):
    path = tmp_path / "config.env"
    original = """LLM_API_KEY=frontend-private
LLM_API_URL=https://frontend.invalid/v1
LLM_MODEL=frontend
MIMO_API_KEY=mimo-private
GAIA2_BACKEND_API_KEY=backend-private
GAIA2_BACKEND_API_BASE=https://backend.invalid/v1
GAIA2_BACKEND_MODEL=backend
QWEN_AUDIO_REALTIME_PROVIDER=dashscope
QWEN_AUDIO_REALTIME_BASE_URL=wss://dashscope.aliyuncs.com/api-ws/v1/realtime
DASHSCOPE_API_KEY3=qwen-private
QWEN_AUDIO_REALTIME_API_KEY=${UNUSED_INACTIVE_KEY}
UNRELATED_JUDGE_KEY=judge-private
"""
    path.write_text(original)
    config = QwenAgentConfig(
        config_env_file=str(path),
        speech_to_speech=SpeechToSpeechConfig(asr_model="qwen-audio-3.1-asr-flash"),
    )
    resolved = config.resolved_environment({"DASHSCOPE_API_KEY3": "ambient-private"})
    assert resolved["TAU_S2S_ASR_API_KEY"] == "ambient-private"
    assert resolved["TAU_S2S_ASR_BASE_URL"] == (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )
    assert resolved["TAU_S2S_MIMO_API_KEY"] == "mimo-private"
    assert resolved["TAU_S2S_LLM_API_KEY"] == "frontend-private"
    assert resolved["OPENAI_API_KEY"] == "backend-private"
    assert resolved["QWEN_AUDIO_REALTIME_PROVIDER"] == "speech-to-speech"
    assert "UNRELATED_JUDGE_KEY" not in resolved
    assert "private" not in config.model_dump_json()
    assert "private" not in resolved["TAU_S2S_CONFIG"]
    assert path.read_text() == original


def test_old_image_is_rejected_before_launching_a_qwen_asr_frontend(monkeypatch):
    from tau2.adapters.qwen_audio_agent.runtime import DockerRuntime

    runtime = DockerRuntime(
        QwenAgentConfig(
            speech_to_speech=SpeechToSpeechConfig(asr_model="qwen-audio-3.1-asr-flash")
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
    with pytest.raises(ValueError, match="Qwen frontend ASR image"):
        runtime.start()
    assert len(commands) == 1 and runtime.container_id is None

"""Qwen caller TTS keeps provider voices through the complete user builder."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.simulation import VoiceRunConfig
from tau2.data_model.voice import QwenTTSConfig, SynthesisConfig
from tau2.domains.gaia2.language_runtime import task_context
from tau2.registry import registry
from tau2.runner.batch import make_voice_run_settings
from tau2.runner.build import build_voice_user
from tau2.voice.mimo import pcm_wav
from tau2.voice.synthesis.synthesize import synthesize_voice


@pytest.mark.parametrize(
    "persona_id,expected",
    [
        ("mamadou_diallo", "Brian_v3.1"),
        ("priya_patil", "loongbella_v3.1"),
        ("unmapped_persona", "loongbella_v3.1"),
    ],
)
def test_qwen_voice_reaches_http_request_after_persona_selection(
    monkeypatch, tmp_path, persona_id, expected
):
    """Reproduce the pack's ElevenLabs ID overwriting a valid Qwen voice."""
    # This builder test uses a constructor double, not local playback hardware.
    monkeypatch.setitem(
        sys.modules,
        "tau2.user.user_simulator_streaming",
        SimpleNamespace(
            VoiceStreamingUserSimulator=lambda **kwargs: SimpleNamespace(**kwargs)
        ),
    )
    secret = tmp_path / "voice.env"
    secret.write_text("QWEN_TEST_KEY=synthetic-secret\n")
    tts = QwenTTSConfig(
        api_key_env="QWEN_TEST_KEY",
        voices={"mamadou_diallo": "Brian_v3.1", "priya_patil": "loongbella_v3.1"},
    )
    system = QwenAgentConfig(config_env_file=str(secret), tts=tts)
    config = VoiceRunConfig(
        domain="gaia2",
        audio_native_config=AudioNativeConfig(),
        speech_complexity="control",
    )
    task = registry.get_tasks_loader("gaia2")("base")[0]
    context = task_context(config, task)
    context = context.model_copy(
        update={
            "persona": context.persona.model_copy(update={"persona_id": persona_id})
        }
    )
    audio = AudioNativeConfig(provider="qwen_agent", qwen_agent_config=system)
    config = config.model_copy(update={"audio_native_config": audio})
    settings, _ = make_voice_run_settings(config)
    user = build_voice_user(
        registry.get_env_constructor("gaia2")(task=task, allow_unverified=True),
        task,
        audio,
        language_context=context,
        voice_settings=settings,
        speech_complexity="control",
    )
    synthesis = SynthesisConfig.model_validate_json(
        user.voice_settings.synthesis_config.model_dump_json()
    )
    assert isinstance(synthesis.provider_config, QwenTTSConfig)
    assert user.voice_settings.speech_environment.voice_id == expected
    assert synthesis.provider_config.voice_id != context.persona.voice.voice_id
    text = "Read [reference] 123 exactly."
    calls = []

    def reply(request):
        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["input"]["voice"] == expected
            assert body["input"]["text"] == text
            assert body["input"]["language_hints"] == ["en"]
            assert body["model"] == "qwen-audio-3.1-tts-flash"
            return httpx.Response(
                200,
                json={"output": {"audio": {"url": "https://audio.invalid/output.wav"}}},
            )
        return httpx.Response(200, content=pcm_wav(b"\x10\x10" * 2400, 24000))

    client_type = httpx.Client
    monkeypatch.setattr(
        "tau2.voice.qwen_tts.httpx.Client",
        lambda **kw: client_type(transport=httpx.MockTransport(reply), **kw),
    )
    result = synthesize_voice(text, synthesis.provider, synthesis.provider_config)
    assert result.format.is_pcm16 and result.format.sample_rate == 16000
    assert result.duration == pytest.approx(0.1, abs=0.001)
    assert calls == ["POST", "GET"]

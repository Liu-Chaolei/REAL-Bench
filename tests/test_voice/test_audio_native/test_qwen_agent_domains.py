"""Domain capability checks happen before credentials or paid model work."""

import json
from types import SimpleNamespace

import pytest

from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime


def test_mimo_user_profile_reaches_simulator_and_decision_model(monkeypatch, tmp_path):
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.multilingual.runtime import task_context
    from tau2.registry import registry
    from tau2.runner.build import build_voice_user

    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.VoiceStreamingUserSimulator",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    path = tmp_path / "config.env"
    path.write_text(
        "MIMO_API_KEY=fake-mimo\nMIMO_BASE_URL=https://mimo.invalid/v1\nMIMO_MODEL=mimo-v2.6-pro\n"
    )
    system = QwenAgentConfig(
        config_env_file=str(path),
        user_llm_api_key_env="MIMO_API_KEY",
        user_llm_base_url_env="MIMO_BASE_URL",
        user_llm_model_env="MIMO_MODEL",
    )
    audio = AudioNativeConfig(provider="qwen_agent", qwen_agent_config=system)
    environment = registry.get_env_constructor("mock")()
    task = registry.get_tasks_loader("mock")()[0]
    context = task_context(
        VoiceRunConfig(domain="mock", audio_native_config=AudioNativeConfig()), task
    )
    user = build_voice_user(environment, task, audio, language_context=context)
    assert user.llm == user.decision_model == "openai/mimo-v2.6-pro"
    assert (
        user.llm_args["api_base"]
        == user.decision_model_args["api_base"]
        == "https://mimo.invalid/v1"
    )
    assert user.llm_args["api_key"] == "fake-mimo"
    assert "fake-mimo" not in system.model_dump_json()


@pytest.mark.parametrize("seconds", [0, 30, 30.05])
def test_caller_silence_checkin_config_reaches_shared_voice_builder(
    monkeypatch, seconds
):
    """The saved pacing setting reaches the user in ticks without rounding down."""
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.multilingual.runtime import task_context
    from tau2.registry import registry
    from tau2.runner.build import build_voice_user

    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.VoiceStreamingUserSimulator",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    environment = registry.get_env_constructor("mock")()
    task = registry.get_tasks_loader("mock")()[0]
    context = task_context(
        VoiceRunConfig(domain="mock", audio_native_config=AudioNativeConfig()), task
    )
    audio = AudioNativeConfig(
        provider="qwen_agent",
        qwen_agent_config=QwenAgentConfig(user_silence_checkin_seconds=seconds),
    )
    user = build_voice_user(environment, task, audio, language_context=context)
    expected_ticks = {0: 0, 30: 150, 30.05: 151}[seconds]
    assert user.silence_checkin_threshold == expected_ticks
    assert user.wait_to_respond_threshold_self == 25
    assert user.wait_to_respond_threshold_other == 5
    assert (
        audio.model_dump()["qwen_agent_config"]["user_silence_checkin_seconds"]
        == seconds
    )


@pytest.mark.parametrize(
    "domain,language,supported",
    [
        ("retail", "en", True),
        ("gaia2", "en", True),
        ("airline", "en", False),
        ("gaia2", "zh", False),
    ],
)
def test_run_config_uses_registered_domain_capabilities(domain, language, supported):
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig

    audio = AudioNativeConfig(
        provider="qwen_agent", qwen_agent_config=QwenAgentConfig()
    )
    if supported:
        config = VoiceRunConfig(
            domain=domain, language=language, audio_native_config=audio
        )
        assert config.max_concurrency == 1
        assert config.max_retries == config.hallucination_retries == 0
    else:
        with pytest.raises(ValueError, match="domain declaring support"):
            VoiceRunConfig(domain=domain, language=language, audio_native_config=audio)


def test_gaia2_rejects_a_historical_retail_only_image(monkeypatch):
    runtime = DockerRuntime(QwenAgentConfig(domain="gaia2"))
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
                        }
                    }
                }
            ]
        )

    monkeypatch.setattr(runtime, "_command", command)
    with pytest.raises(ValueError, match="support for this domain"):
        runtime.start()
    assert len(commands) == 1
    assert commands[0][0:2] == ("image", "inspect")
    assert runtime.container_id is None


def test_explicit_parallelism_preserves_retry_restrictions():
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig

    audio = AudioNativeConfig(
        provider="qwen_agent", qwen_agent_config=QwenAgentConfig()
    )
    config = VoiceRunConfig(
        domain="gaia2", audio_native_config=audio, max_concurrency=5
    )
    assert config.max_concurrency == 5
    assert config.max_retries == config.hallucination_retries == 0
    with pytest.raises(ValueError, match="max_retries"):
        VoiceRunConfig(
            domain="gaia2", audio_native_config=audio, max_concurrency=5, max_retries=1
        )

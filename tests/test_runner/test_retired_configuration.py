"""New runs reject removed settings while saved trajectories remain readable."""

import argparse

import pytest

from tau2.agent.discrete_time_audio_native_agent import DiscreteTimeAudioNativeAgent
from tau2.cli import add_run_args
from tau2.data_model.run_config import AudioNativeConfig, RecordedAudioNativeConfig
from tau2.data_model.simulation import Info, Results, TextRunConfig, VoiceRunConfig
from tau2.runner.checkpoint import try_resume
from tau2.voice.audio_native.adapter import create_adapter


def saved_results(provider):
    audio = {"provider": provider, "model": "historical-model"}
    if provider == "openai_live":
        audio["live_config"] = {"backend_model": "historical-backend"}
    return Results.model_validate(
        {
            "info": {
                "git_commit": "historical",
                "num_trials": 1,
                "max_steps": 10,
                "max_errors": 1,
                "agent_info": {"implementation": "audio_native"},
                "user_info": {"implementation": "voice"},
                "environment_info": {"domain_name": "gaia2", "policy": "policy"},
                "audio_native_config": audio,
                "retrieval_config": "historical-retrieval",
            },
            "tasks": [],
            "simulations": [],
        }
    )


@pytest.mark.parametrize("provider", ["openai_live", "gemini"])
def test_retired_provider_is_readable_but_cannot_start_or_resume(provider, tmp_path):
    historical = saved_results(provider)
    path = tmp_path / "results.json"
    historical.save(path)
    restored = Results.load(path)
    assert restored.info.audio_native_config.provider == provider
    assert restored.info.retrieval_config == "historical-retrieval"
    if provider == "openai_live":
        assert restored.info.audio_native_config.live_config.backend_model

    with pytest.raises(ValueError):
        AudioNativeConfig(provider=provider)
    with pytest.raises(ValueError, match="retired or unsupported"):
        VoiceRunConfig(audio_native_config=restored.info.audio_native_config)
    with pytest.raises(ValueError, match="retired or unsupported"):
        create_adapter(provider, tick_duration_ms=200)
    with pytest.raises(ValueError, match="retired or unsupported"):
        DiscreteTimeAudioNativeAgent(tools=[], domain_policy="", provider=provider)
    with pytest.raises(ValueError, match="Cannot resume retired"):
        try_resume(path, restored, [], 1, auto_resume=True)


@pytest.mark.parametrize(
    "setting", ["retrieval_config", "retrieval_config_kwargs", "elicitation"]
)
def test_new_gaia2_runs_reject_old_domain_settings(setting):
    with pytest.raises(ValueError, match=setting):
        TextRunConfig.model_validate({setting: "obsolete"})


def test_live_settings_cannot_be_smuggled_through_recorded_config():
    audio = RecordedAudioNativeConfig(
        provider="openai", live_config={"backend_model": "historical"}
    )
    with pytest.raises(ValueError, match="live_config is retired"):
        VoiceRunConfig(audio_native_config=audio)
    with pytest.raises(ValueError, match="live_config"):
        AudioNativeConfig(provider="openai", live_config={"backend_model": "old"})


def test_cli_exposes_current_providers_and_no_live_config():
    parser = argparse.ArgumentParser()
    add_run_args(parser)
    actions = {action.dest: action for action in parser._actions}
    providers = actions["audio_native_provider"].choices
    assert {"openai", "qwen_agent"} <= set(providers)
    assert not {"gemini", "openai_live"} & set(providers)
    assert (
        not {"live_config", "retrieval_config", "retrieval_config_kwargs"}
        & actions.keys()
    )
    assert VoiceRunConfig(audio_native_config=AudioNativeConfig(provider="openai"))
    recorded = Info.read_audio_config(AudioNativeConfig(provider="openai"))
    assert RecordedAudioNativeConfig.model_validate(recorded).provider == "openai"

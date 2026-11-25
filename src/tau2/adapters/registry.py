"""Lazy factories and static behavior for tested Agent adapters."""

from importlib import import_module

from pydantic import BaseModel, ConfigDict

from tau2.data_model.asr import HotwordSettings
from tau2.data_model.audio import AudioFormat


class AdapterBehavior(BaseModel):
    """Static integration choices; reading them must not open a session."""

    model_config = ConfigDict(frozen=True)
    policy_only: bool = False
    initial_greeting: bool = True
    flush_results_on_stop: bool = False
    hosted_turn_detection: bool = False
    playback_audio_format: AudioFormat | None = None
    protocol_trace: bool = False


class ObserverPreparation(BaseModel):
    """Public observer requests; material selection remains on evaluation side."""

    hotwords: HotwordSettings
    api_config_dir: str | None = None


_FACTORIES = {"qwen_agent": "tau2.adapters.qwen_audio_agent.factory"}


def adapter_module(provider):
    """Resolve only the selected adapter's construction module."""
    name = _FACTORIES.get(provider)
    return import_module(name) if name else None


def adapter_behavior(provider, config=None, audio_format=None):
    """Describe a selected Agent without constructing its transport."""
    module = adapter_module(provider)
    return module.behavior(config, audio_format) if module else AdapterBehavior()


def create_registered_adapter(provider, **kwargs):
    """Create a fresh selected adapter; no connected instances are cached."""
    module = adapter_module(provider)
    if module is None:
        raise ValueError(f"No registered adapter: {provider}")
    return module.create(**kwargs)


def observer_preparation(provider, config):
    """Describe needed observer preparation without exposing an Agent internals."""
    module = adapter_module(provider)
    return module.observer_preparation(config) if module else None


def prepare_adapter_config(provider, config, *, domain, notification_wait_seconds=None):
    """Supply only public environment parameters needed by a selected adapter."""
    module = adapter_module(provider)
    return (
        module.prepare_config(
            config, domain=domain, notification_wait_seconds=notification_wait_seconds
        )
        if module
        else config
    )

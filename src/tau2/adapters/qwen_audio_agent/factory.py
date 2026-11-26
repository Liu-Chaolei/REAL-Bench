"""Qwen construction without connecting a Provider or starting a container."""

from tau2.adapters.qwen_audio_agent.config import QWEN_AGENT_INPUT_AUDIO_FORMAT
from tau2.data_model.audio import AudioEncoding, AudioFormat


def behavior(config, audio_format=None):
    """Describe the external system's prompt, turn and playback behavior."""
    from tau2.adapters.registry import AdapterBehavior

    playback = None
    if config is not None:
        playback = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE, sample_rate=config.playback_sample_rate
        )
    return AdapterBehavior(
        policy_only=True,
        initial_greeting=False,
        flush_results_on_stop=True,
        hosted_turn_detection=True,
        input_audio_format=QWEN_AGENT_INPUT_AUDIO_FORMAT,
        playback_audio_format=playback,
        protocol_trace=True,
    )


def observer_preparation(config):
    """Request only the existing hotword selection on the evaluator side."""
    from tau2.adapters.registry import ObserverPreparation

    return ObserverPreparation(
        hotwords=config.asr_hotwords, api_config_dir=config.api_config_dir
    )


def prepare_config(config, *, domain, notification_wait_seconds=None):
    """Bind public domain/timeout requirements without receiving a task or DB."""
    if config is None:
        raise ValueError("qwen_agent requires qwen_agent_config")
    if (
        notification_wait_seconds is not None
        and config.tool_timeout_seconds <= notification_wait_seconds + 5
    ):
        raise ValueError(
            "Timed scenarios require a Qwen MCP tool timeout greater than the shared notification wait plus five seconds"
        )
    return config.model_copy(update={"domain": domain})


def create(*, qwen_agent_config, **kwargs):
    """Construct one Qwen adapter, leaving connection to its lifecycle owner."""
    from tau2.adapters.qwen_audio_agent.adapter import DiscreteTimeQwenAgentAdapter

    if qwen_agent_config is None:
        raise ValueError("qwen_agent requires qwen_agent_config")
    return DiscreteTimeQwenAgentAdapter(config=qwen_agent_config, **kwargs)

"""Qwen adaptation boundaries use only local doubles, without Docker or live APIs."""

import importlib
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
from tau2.adapters.registry import (
    adapter_behavior,
    create_registered_adapter,
    prepare_adapter_config,
)
from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.user_simulation import UserSimulationConfig


@pytest.mark.parametrize(
    "old,new",
    [
        (
            "tau2.voice.audio_native.qwen_agent.config",
            "tau2.adapters.qwen_audio_agent.config",
        ),
        (
            "tau2.voice.audio_native.qwen_agent.discrete_time_adapter",
            "tau2.adapters.qwen_audio_agent.adapter",
        ),
        (
            "tau2.voice.audio_native.qwen_agent.runtime",
            "tau2.adapters.qwen_audio_agent.runtime",
        ),
        (
            "tau2.persistent_tasks.adapters.qwen",
            "tau2.adapters.qwen_audio_agent.events",
        ),
    ],
)
def test_old_import_is_the_same_implementation(old, new):
    assert importlib.import_module(old) is importlib.import_module(new)


def test_non_qwen_selection_does_not_import_qwen_transport():
    code = """
import sys
from tau2.adapters.registry import adapter_behavior
assert adapter_behavior('xai').initial_greeting
assert 'tau2.adapters.qwen_audio_agent.adapter' not in sys.modules
assert 'tau2.adapters.qwen_audio_agent.runtime' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP="True"),
    )
    assert result.returncode == 0, result.stderr


def test_qwen_factory_creates_separate_unconnected_resources(monkeypatch):
    from tau2.adapters.qwen_audio_agent.runtime import DockerRuntime

    def forbidden(*args):
        pytest.fail("Construction must not start Docker")

    monkeypatch.setattr(DockerRuntime, "start", forbidden)
    config = QwenAgentConfig()
    adapters = [
        create_registered_adapter(
            "qwen_agent",
            tick_duration_ms=200,
            qwen_agent_config=config,
            transcriber=SimpleNamespace(summary=lambda: {}),
        )
        for _ in range(2)
    ]
    left, right = adapters
    assert left.provider is not right.provider
    assert left.provider.runtime is not right.provider.runtime
    assert left.provider.runtime.runtime_label != right.provider.runtime.runtime_label
    assert left.provider.backend_events is not right.provider.backend_events
    assert not left._bg_loop.is_running
    left.enable_diagnostics()
    assert left.provider.diagnostics_enabled
    assert not right.provider.diagnostics_enabled
    assert left.get_diagnostic_evidence()["status"] == "unavailable"
    left.provider.backend_observer_ready = True
    left.provider.backend_inputs["i"] = {"content": "observed"}
    assert left.get_diagnostic_evidence()["backend_inputs"] == [{"content": "observed"}]
    assert left.get_run_metadata()["cost_available"] is False
    assert left.get_async_evidence_source().get_capabilities()["task_identity"]
    left.disconnect()
    left.disconnect()
    right.disconnect()


def test_generic_agent_delegates_prompt_greeting_and_stop_behavior():
    from tau2.agent.discrete_time_audio_native_agent import DiscreteTimeAudioNativeAgent
    from tau2.data_model.message import ToolMessage

    calls = []
    transport = SimpleNamespace(
        send_tool_result=lambda **kwargs: calls.append(kwargs),
        finish_media=lambda: None,
        disconnect=lambda: calls.append("closed"),
        enable_diagnostics=lambda: calls.append("diagnostics"),
        get_diagnostic_evidence=lambda: {"status": "complete"},
        get_run_metadata=lambda: {"system_metadata": {"adapter": "qwen"}},
    )
    agent = DiscreteTimeAudioNativeAgent(
        tools=[],
        domain_policy="public policy",
        provider="qwen_agent",
        qwen_agent_config=QwenAgentConfig(),
    )
    assert agent._adapter is None
    assert agent.system_prompt == "public policy"
    assert agent.create_initial_message().content is None
    agent.enable_diagnostics()
    assert agent._adapter is None
    agent._adapter = transport
    agent.stop(
        tool_results=ToolMessage(
            role="tool", id="c", name="lookup", content="result", requestor="assistant"
        )
    )
    assert calls[-1] == "closed"
    sent = next(r for r in calls if isinstance(r, dict))
    assert sent["call_id"] == "c" and sent["request_response"] is False
    assert agent.get_diagnostic_evidence()["status"] == "complete"
    assert agent.get_run_metadata()["system_metadata"]["adapter"] == "qwen"


def test_public_domain_timeout_binding_keeps_original_config():
    original = QwenAgentConfig(tool_timeout_seconds=40)
    prepared = prepare_adapter_config(
        "qwen_agent", original, domain="gaia2", notification_wait_seconds=30
    )
    assert prepared.domain == "gaia2" and original.domain is None
    with pytest.raises(ValueError, match="MCP tool timeout"):
        prepare_adapter_config(
            "qwen_agent", original, domain="gaia2", notification_wait_seconds=40
        )


def test_legacy_caller_settings_resolve_once_outside_adapter():
    config = AudioNativeConfig(
        provider="qwen_agent",
        qwen_agent_config=QwenAgentConfig(
            user_decision_llm="judge",
            user_silence_checkin_seconds=12,
            dynamic_followup={"enabled": True},
            watchdog={"enabled": True},
        ),
    )
    assert config.user_simulation.user_decision_llm == "judge"
    assert config.user_simulation.dynamic_followup.enabled
    assert config.user_simulation.watchdog.enabled
    assert config.user_simulation_origin == "legacy_qwen"
    assert config.qwen_agent_config.evaluation is None
    assert "dynamic_followup" not in type(config.qwen_agent_config).model_fields
    assert AudioNativeConfig.model_validate_json(config.model_dump_json()) == config


def test_caller_settings_work_without_qwen_and_preserve_neutral_defaults():
    config = AudioNativeConfig(
        provider="xai",
        user_simulation=UserSimulationConfig(
            dynamic_followup={"enabled": True},
            user_silence_checkin_seconds=8,
        ),
    )
    assert config.qwen_agent_config is None
    assert config.user_simulation.dynamic_followup.enabled
    assert config.user_simulation_origin == "explicit"
    assert "user_simulation" not in AudioNativeConfig().model_dump()
    assert adapter_behavior("xai").initial_greeting


def test_conflicting_new_and_legacy_caller_settings_are_rejected():
    with pytest.raises(ValueError, match="conflicts with user_simulation"):
        AudioNativeConfig(
            provider="qwen_agent",
            qwen_agent_config=QwenAgentConfig(user_silence_checkin_seconds=12),
            user_simulation={"user_silence_checkin_seconds": 20},
        )
    config = AudioNativeConfig(
        provider="qwen_agent",
        qwen_agent_config=QwenAgentConfig(),
        user_simulation={"user_silence_checkin_seconds": 0},
    )
    assert config.user_simulation.user_silence_checkin_seconds == 0

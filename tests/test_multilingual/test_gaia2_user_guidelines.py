"""Offline construction checks for Gaia2's neutral user role."""

from types import SimpleNamespace

import pytest

from tau2.data_model.language import LanguageDirective
from tau2.data_model.message import AssistantMessage
from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.simulation import VoiceRunConfig
from tau2.data_model.tasks import Task, UserScenario
from tau2.data_model.voice import SampledVoiceConfig, SynthesisConfig, VoiceSettings
from tau2.multilingual.runtime import load_pack, resolve_persona, task_context
from tau2.runner.build import build_user, build_voice_user
from tau2.user.gaia2_guidelines import (
    GAIA2_ROLE_REMINDER,
    GAIA2_USER_GUIDELINES_VERSION,
)

REQUEST = "Message Helga about the $7.95 hat and set a reminder for Friday at 10 AM."


def environment(domain):
    return SimpleNamespace(
        get_domain_name=lambda: domain, get_user_tools=lambda **_: []
    )


def construct(mode, domain, monkeypatch, *, cross_lingual=False):
    """Use real constructors; isolate only voice sampling and billable speech."""
    persona = resolve_persona(load_pack("en"), "priya_patil", mode=mode)
    persona = persona.model_copy(
        update={
            "verbosity": "minimal",
            "user_guidelines": "Only approve actions that implement the current request.",
            "mode_guidelines": "Keep monetary amounts in USD.",
            **(
                {
                    "display_language": "Example language",
                    "language_directive": LanguageDirective(origin="Example region"),
                }
                if cross_lingual
                else {}
            ),
        }
    )
    task = Task(
        id="0760-neutral-role-fixture", user_scenario=UserScenario(instructions=REQUEST)
    )
    before = persona.model_dump(mode="json")
    if mode == "text":
        user = build_user(
            "user_simulator",
            environment(domain),
            task,
            llm="unused",
            persona_config=persona,
        )
        return user, persona, before, None

    monkeypatch.setattr(
        "tau2.voice.synthesis.audio_effects.speech_generator.OutOfTurnSpeechGenerator.generate_all",
        lambda *_args, **_kwargs: None,
    )

    def sample(**kwargs):
        synthesis = kwargs["synthesis_config"]
        return SampledVoiceConfig(
            persona_name=persona.persona_id,
            persona_config=persona,
            complexity="control",
            use_llm_backchannel=False,
            channel_effects_config=synthesis.channel_effects_config,
            source_effects_config=synthesis.source_effects_config,
            speech_effects_config=synthesis.speech_effects_config,
        )

    monkeypatch.setattr("tau2.runner.build.get_or_load_task_voice_config", sample)
    settings = VoiceSettings(
        transcription_config=None, synthesis_config=SynthesisConfig()
    )
    original = settings.model_dump(mode="json")
    context = task_context(
        VoiceRunConfig(domain="mock", audio_native_config=AudioNativeConfig()), task
    ).model_copy(update={"persona": persona})
    user = build_voice_user(
        environment(domain),
        task,
        AudioNativeConfig(),
        language_context=context,
        voice_settings=settings,
        llm="unused",
        speech_complexity="control",
    )
    assert settings.model_dump(mode="json") == original
    return user, persona, before, settings


@pytest.mark.parametrize("mode", ["text", "voice"])
@pytest.mark.parametrize("cross_lingual", [False, True])
def test_gaia2_complete_prompt_and_role_reminder(
    mode, cross_lingual, monkeypatch, voice_extra
):
    user, persona, before, _ = construct(
        mode, "gaia2", monkeypatch, cross_lingual=cross_lingual
    )
    prompt = user.system_prompt
    assert GAIA2_USER_GUIDELINES_VERSION in prompt
    assert "human user interacting with an AI agent" in prompt
    assert REQUEST in prompt and "female user" in prompt
    assert "MINIMAL VERBOSITY" in prompt
    assert persona.user_guidelines in prompt and persona.mode_guidelines in prompt
    assert not any(
        word in prompt.lower()
        for word in ["customer", "supervisor", "make the agent work", "forget details"]
    )
    assert all(
        token in prompt
        for token in ["###STOP###", "###OUT-OF-SCOPE###"]
    )
    if cross_lingual:
        assert (
            "native Example language speaker" in prompt
            and "## FINAL REMINDER" in prompt
        )
    if mode == "voice":
        assert persona.spellout_section.strip() in prompt
        calls = []

        def generate(**kwargs):
            calls.append(kwargs)
            return AssistantMessage(role="assistant", content="###STOP###")

        monkeypatch.setattr("tau2.user.user_simulator_streaming.generate", generate)
        message, _ = user._generate_full_duplex_voice_message(
            None, user.get_init_state()
        )
        assert message.content == "###STOP###" and len(calls) == 1
        assert calls[0]["messages"][-1].content == GAIA2_ROLE_REMINDER
    assert persona.model_dump(mode="json") == before


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_domain_role_selection_preserves_original_persona_and_acoustics(
    mode, monkeypatch, voice_extra
):
    neutral, persona, before, _ = construct(mode, "gaia2", monkeypatch)
    service, service_persona, service_before, _ = construct(mode, "retail", monkeypatch)
    assert "customer service representative" in service.system_prompt
    assert "supervisor" in service.system_prompt
    assert service.global_guidelines_override is None
    assert neutral.persona_config == service.persona_config
    assert persona.model_dump(mode="json") == before
    assert service_persona.model_dump(mode="json") == service_before
    if mode == "voice":
        assert neutral.voice_settings == service.voice_settings
        assert (
            neutral.persona_config.interrupt_tendency
            == service.persona_config.interrupt_tendency
        )
        assert service.role_reminder_override is None

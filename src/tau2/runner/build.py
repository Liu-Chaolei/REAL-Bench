"""
Layer 2: Build functions.

Turn config/names into live instances (environment, agent, user, orchestrator).
Uses the registry for name resolution. Callers who want full control can skip
this layer and construct instances directly.
"""

import hashlib
import json
import uuid
from contextlib import ExitStack
from copy import deepcopy
from math import ceil
from pathlib import Path
from typing import Optional, Union

from loguru import logger

from tau2.agent.base_agent import FullDuplexAgent, HalfDuplexAgent
from tau2.backchannel import BackchannelSettings, default_backchannel_settings
from tau2.config import (
    DEFAULT_CHANNEL_EFFECTS_MODE,
    DEFAULT_SPEECH_EFFECTS_MODE,
)
from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.data_model.language import CallContext, LanguageTaskInfo
from tau2.data_model.persona import PersonaConfig
from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.simulation import RunConfig, TextRunConfig, VoiceRunConfig
from tau2.data_model.tasks import Task
from tau2.data_model.voice import (
    SpeechComplexity,
    SpeechEnvironment,
    SynthesisConfig,
    VoiceSettings,
)
from tau2.environment.environment import Environment
from tau2.environment.tool import Tool
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.orchestrator.orchestrator import Orchestrator
from tau2.registry import registry
from tau2.user.user_simulator import DummyUser, UserSimulator
from tau2.user.user_simulator_base import FullDuplexUser, HalfDuplexUser
from tau2.user_simulation_voice_presets import (
    VoiceSelection,
    get_or_load_task_voice_config,
)

# =============================================================================
# Low-level build functions (no RunConfig needed)
# =============================================================================


def build_environment(
    domain: str,
    *,
    solo_mode: bool = False,
    env_kwargs: Optional[dict] = None,
) -> Environment:
    """Build an environment from a domain name.

    Uses the registry to resolve the domain name to an environment constructor.

    Args:
        domain: Registered domain name (currently "gaia2").
        solo_mode: If True, environment is built in solo mode (agent gets
            access to both agent and user tools).
        env_kwargs: Additional keyword arguments passed to the environment
            constructor (e.g., the Gaia2 task).

    Returns:
        A fully constructed Environment instance.
    """
    env_constructor = registry.get_env_constructor(domain)
    kwargs = dict(env_kwargs or {})
    if solo_mode:
        kwargs["solo_mode"] = True
    return env_constructor(**kwargs)


def agent_domain_policy(
    environment: Environment, language_guidance: str = "", *, language: str = "en"
) -> str:
    """Attach resolved language guidance to the ordinary domain policy."""
    policy = environment.get_policy()
    return policy + ("\n\n" + language_guidance if language_guidance else "")


def user_tools_for_task(environment: Environment, task: Task) -> Optional[list[Tool]]:
    """Select the same optional caller tools for execution and prompt review."""
    try:
        return environment.get_user_tools(include=task.user_tools) or None
    except Exception:
        return None


def build_agent(
    agent_name: str,
    environment: Environment,
    *,
    llm: Optional[str] = None,
    llm_args: Optional[dict] = None,
    task: Optional[Task] = None,
    audio_native_config: Optional[AudioNativeConfig] = None,
    solo_mode: bool = False,
    audio_taps_dir: Optional[Path] = None,
    language_guidance: str = "",
    language: str = "en",
) -> Union[HalfDuplexAgent, FullDuplexAgent]:
    """Build an agent from a registered name and an environment.

    Uses the registry to resolve the agent name to a factory function,
    then calls it with the appropriate parameters.

    Args:
        agent_name: Registered agent name (e.g., "llm_agent", "llm_agent_gt",
            "discrete_time_audio_native_agent", or "experimental:my_agent").
        environment: The environment to extract tools and policy from.
        llm: LLM model name for the agent (half-duplex agents).
        llm_args: LLM arguments for the agent (half-duplex agents).
        task: The task (required for some agents like llm_agent_gt, llm_agent_solo).
        audio_native_config: Audio config (full-duplex agents).
        solo_mode: If True, agent tools include both agent and user tools.

    Returns:
        A fully constructed agent instance.

    Raises:
        ValueError: If the agent name has no factory registered.
    """
    agent_factory = registry.get_agent_factory(agent_name)
    if agent_factory is None:
        raise ValueError(
            f"Agent '{agent_name}' has no factory registered. "
            f"Register a factory with registry.register_agent_factory()."
        )

    # Collect tools from environment
    tools = environment.get_tools()
    if solo_mode:
        try:
            user_tools = environment.get_user_tools()
            if user_tools:
                tools = tools + user_tools
        except Exception:
            pass

    domain_kwargs = {}
    domain = environment.get_domain_name()
    if audio_native_config is not None:
        from tau2.adapters.registry import prepare_adapter_config
        from tau2.config import DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS

        prepared = prepare_adapter_config(
            audio_native_config.provider,
            audio_native_config.qwen_agent_config,
            domain=domain,
            notification_wait_seconds=(
                DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS
                if getattr(environment, "scenario_runtime", None) is not None
                else None
            ),
        )
        if prepared is not audio_native_config.qwen_agent_config:
            audio_native_config = audio_native_config.model_copy(
                update={"qwen_agent_config": prepared}
            )

    return agent_factory(
        tools=tools,
        domain_policy=agent_domain_policy(
            environment, language_guidance, language=language
        ),
        llm=llm,
        llm_args=llm_args,
        task=task,
        audio_native_config=audio_native_config,
        audio_taps_dir=audio_taps_dir,
        **domain_kwargs,
    )


def build_user(
    user_name: str,
    environment: Environment,
    task: Task,
    *,
    llm: Optional[str] = None,
    llm_args: Optional[dict] = None,
    persona_config: Optional[PersonaConfig] = None,
    solo_mode: bool = False,
    hidden_intent_graph=None,
) -> HalfDuplexUser:
    """Build a half-duplex user from a registered name.

    Uses the registry to resolve the user name to a constructor.

    Args:
        user_name: Registered user name (e.g., "user_simulator", "dummy_user").
        environment: The environment to extract user tools from.
        task: The task (used for user instructions).
        llm: LLM model name for the user simulator.
        llm_args: LLM arguments for the user simulator.
        persona_config: Persona configuration (verbosity, interrupt tendency).
        solo_mode: If True, validates that DummyUser is used appropriately.

    Returns:
        A fully constructed half-duplex user instance.

    Raises:
        AssertionError: If DummyUser is used without solo_mode.
    """
    UserConstructor = registry.get_user_constructor(user_name)

    user_tools = user_tools_for_task(environment, task)

    # Validate DummyUser usage
    if issubclass(UserConstructor, DummyUser):
        assert solo_mode, "Dummy user can only be used with solo agent"
        # The solo agent plays both sides; the dummy user takes no configuration.
        return UserConstructor()

    user_kwargs = {
        "tools": user_tools,
        "instructions": str(task.user_scenario),
        "llm": llm,
        "llm_args": llm_args,
    }
    if issubclass(UserConstructor, UserSimulator):
        user_kwargs["persona_config"] = persona_config
        user_kwargs["hidden_intent_graph"] = hidden_intent_graph
        if environment.get_domain_name() == "gaia2":
            from tau2.user.gaia2_guidelines import get_gaia2_user_guidelines

            user_kwargs["global_guidelines_override"] = get_gaia2_user_guidelines(
                persona_config, mode="text", use_tools=user_tools is not None
            )

    return UserConstructor(**user_kwargs)


def build_voice_user(
    environment: Environment,
    task: Task,
    audio_native_config: AudioNativeConfig,
    *,
    language_context: LanguageTaskInfo,
    llm: Optional[str] = None,
    llm_args: Optional[dict] = None,
    voice_settings: Optional[VoiceSettings] = None,
    persona_config: Optional[PersonaConfig] = None,
    speech_complexity: SpeechComplexity = "regular",
    seed: int = 42,
    domain: Optional[str] = None,
    hallucination_feedback: Optional[str] = None,
    audio_taps_dir: Optional[Path] = None,
    channel_effects_mode: str = DEFAULT_CHANNEL_EFFECTS_MODE,
    speech_effects_mode: str = DEFAULT_SPEECH_EFFECTS_MODE,
    force_sample_voice_config: bool = False,
    saasr_session=None,
    dynamic_followup_config: DynamicFollowupConfig | None = None,
    hidden_intent_graph=None,
) -> FullDuplexUser:
    """Build a full-duplex voice user simulator.

    Handles all voice configuration wiring: sampling voice configs per task,
    merging effect configs, creating speech environment, and constructing the
    VoiceStreamingUserSimulator with all timing parameters from audio_native_config.

    Args:
        environment: The environment to extract user tools from.
        task: The task (used for user instructions and voice config sampling).
        audio_native_config: Full audio-native configuration (timing, thresholds, etc.).
        llm: LLM model name for the user simulator.
        llm_args: LLM arguments for the user simulator.
        voice_settings: Base voice settings. If None, defaults are created.
            Deep copied internally to avoid mutation.
        language_context: Resolved voice-mode language context; its pack persona
            voices the caller unless the domain owns the caller.
        persona_config: Persona for domain-owned callers. If None, derived from
            the sampled voice config.
        speech_complexity: Speech environment complexity level.
        seed: Base seed for voice config sampling, offset by a stable digest of
            the canonical task ID so draws align across processes and languages.
        domain: Domain name (used for loading pre-sampled voice configs).
            If None, extracted from environment.
        hallucination_feedback: Optional feedback from a previous hallucination
            check. If provided, appended to user instructions to help avoid
            repeating the same errors on retry.

    Returns:
        A fully constructed VoiceStreamingUserSimulator.
    """
    if domain is None:
        domain = environment.get_domain_name()

    user_tools = user_tools_for_task(environment, task)

    # Set up voice settings (deep copy to avoid mutating caller's settings)
    if voice_settings is not None:
        task_voice_settings = deepcopy(voice_settings)
    else:
        task_voice_settings = VoiceSettings(
            transcription_config=None,
            synthesis_config=SynthesisConfig(),
        )

    if language_context.mode != "voice":
        raise ValueError("voice users require a voice-mode language context")
    persona = language_context.persona
    selection = None
    if persona is not None:
        from tau2.data_model.audio_effects import UserSpeechInsert

        selection = VoiceSelection(
            persona_name=persona.persona_id,
            persona_config=persona,
            non_directed_phrases=[
                UserSpeechInsert(text=text, type="non_directed_phrase")
                for text in persona.voice.non_directed_phrases
            ],
            vocal_tics=[
                UserSpeechInsert(text=text, type="vocal_tic")
                for text in persona.voice.vocal_tics
            ],
            speech_insert_events_per_minute=language_context.speech_insert_events_per_minute,
        )

    # Keep retries stable across processes and acoustic draws aligned across
    # languages through the canonical domain task identity.
    source_id = language_context.canonical_task_id
    task_seed = (
        seed
        + int.from_bytes(hashlib.sha256(source_id.encode()).digest()[:8], "big")
        % 1000000
    )
    sampled_voice_config = get_or_load_task_voice_config(
        domain=domain,
        task_id=source_id,
        task_seed=task_seed,
        complexity=speech_complexity,
        synthesis_config=task_voice_settings.synthesis_config,
        selection=selection,
        channel_effects_mode=channel_effects_mode,
        speech_effects_mode=speech_effects_mode,
        force_sample=force_sample_voice_config,
    )

    if persona is not None:
        synthesis = task_voice_settings.synthesis_config
        synthesis.spelled_entity_tables = persona.voice.spelled_entity_tables
        synthesis.provider_config.language_code = language_context.language.split("-")[
            0
        ]
        if synthesis.provider in {"mimo", "qwen"}:
            # Pack voice IDs belong to ElevenLabs. Other providers must keep
            # their own configured voice or explicit per-persona mapping.
            synthesis.provider_config.voice_id = synthesis.provider_config.voices.get(
                persona.persona_id, synthesis.provider_config.voice_id
            )
        else:
            synthesis.provider_config.voice_id = persona.voice.voice_id

    # Update synthesis_config with merged effect configs
    task_voice_settings.synthesis_config.channel_effects_config = (
        sampled_voice_config.channel_effects_config
    )
    task_voice_settings.synthesis_config.source_effects_config = (
        sampled_voice_config.source_effects_config
    )
    task_voice_settings.synthesis_config.speech_effects_config = (
        sampled_voice_config.speech_effects_config
    )

    # Set speech environment
    if persona is not None:
        speech_environment = SpeechEnvironment(
            **sampled_voice_config.model_dump(exclude={"persona_config"}),
            voice_seed=task_seed,
            voice_id=task_voice_settings.synthesis_config.provider_config.voice_id,
        )
        persona_config = persona
    else:
        speech_environment = sampled_voice_config.to_speech_environment(task_seed)
        persona_config = persona_config or sampled_voice_config.persona_config
    task_voice_settings.speech_environment = speech_environment
    interaction_insert_config = None
    if saasr_session is not None:
        # Preserve the independent TTS settings used only for short interaction
        # inserts before the main caller renderer is switched to SAASR.
        interaction_insert_config = deepcopy(task_voice_settings.synthesis_config)
        # Record the actual acoustic assignment rather than an unused legacy TTS voice.
        synthesis = task_voice_settings.synthesis_config
        synthesis.provider = "saasr"
        synthesis.provider_config = None
        reference = saasr_session.scene["speaker_refs"][0]
        speech_environment.voice_id = f"saasr:reference:{reference}"
        speech_environment.background_noise_file = None
        speech_environment.burst_noise_files = []
        speech_environment.telephony_enabled = False
        speech_environment.environment = "saasr-continuous-v2"

    # Crossed speech effects change caller behavior while retaining the
    # selected pack persona's identity and gender.
    if speech_effects_mode != "regular":
        persona_config = sampled_voice_config.persona_config
    user_instructions = str(task.user_scenario)
    if hallucination_feedback:
        user_instructions += f"\n\n{hallucination_feedback}"

    from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator

    domain_kwargs = {}
    if environment.get_domain_name() == "gaia2":
        from tau2.user.gaia2_guidelines import (
            GAIA2_ROLE_REMINDER,
            get_gaia2_user_guidelines,
        )

        domain_kwargs["global_guidelines_override"] = get_gaia2_user_guidelines(
            persona_config, mode="voice", use_tools=user_tools is not None
        )
        domain_kwargs["role_reminder_override"] = GAIA2_ROLE_REMINDER

    if audio_native_config.user_simulation is not None:
        system = audio_native_config.user_simulation
        caller_llm = system.user_llm_settings()
        if system.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            decision = get_api_configuration(system.api_config_dir).text(
                "USER_DECISION"
            )
            domain_kwargs["api_config_dir"] = system.api_config_dir
        if caller_llm:
            llm, llm_args = caller_llm
        domain_kwargs["silence_checkin_threshold"] = ceil(
            system.user_silence_checkin_seconds
            / audio_native_config.tick_duration_seconds
        )
        if caller_llm:
            domain_kwargs["decision_model"] = (
                decision.litellm_model if system.api_config_dir else llm
            )
            domain_kwargs["decision_model_args"] = (
                decision.arguments() if system.api_config_dir else dict(llm_args)
            )
        elif system.user_decision_llm:
            domain_kwargs["decision_model"] = system.user_decision_llm
            domain_kwargs["decision_model_args"] = dict(system.user_decision_llm_args)

    user = VoiceStreamingUserSimulator(
        tools=user_tools,
        instructions=user_instructions,
        llm=llm,
        llm_args=llm_args,
        voice_settings=task_voice_settings,
        **domain_kwargs,
        chunk_size=audio_native_config.user_chunk_size,
        wait_to_respond_threshold_other=audio_native_config.wait_to_respond_threshold_other_ticks,
        wait_to_respond_threshold_self=audio_native_config.wait_to_respond_threshold_self_ticks,
        yield_threshold_when_interrupted=audio_native_config.yield_threshold_when_interrupted_ticks,
        yield_threshold_when_interrupting=audio_native_config.yield_threshold_when_interrupting_ticks,
        backchannel_min_threshold=(
            int(
                sampled_voice_config.backchannel_min_threshold
                / audio_native_config.tick_duration_seconds
            )
            if sampled_voice_config.backchannel_min_threshold is not None
            else None
        ),
        backchannel_max_threshold=audio_native_config.backchannel_max_threshold_ticks,
        backchannel_poisson_rate=audio_native_config.backchannel_poisson_rate,
        use_llm_backchannel=sampled_voice_config.use_llm_backchannel,
        interruption_check_interval=audio_native_config.interruption_check_interval_ticks,
        integration_ticks=audio_native_config.integration_ticks,
        silence_annotation_threshold_ticks=audio_native_config.silence_annotation_threshold_ticks,
        tick_duration_seconds=audio_native_config.tick_duration_seconds,
        persona_config=persona_config,
        backchannel=backchannel_settings(language_context),
        audio_taps_dir=audio_taps_dir,
        realtime_generation=audio_native_config.realtime_generation_enabled,
        saasr_session=saasr_session,
        interaction_insert_config=interaction_insert_config,
        dynamic_followup_config=dynamic_followup_config,
        hidden_intent_graph=hidden_intent_graph,
    )
    return user


# =============================================================================
# High-level build functions (use RunConfig)
# =============================================================================


def backchannel_settings(context: LanguageTaskInfo) -> BackchannelSettings:
    """The call's backchannel density and continuers from its language pack."""
    if context.persona is not None:
        return BackchannelSettings(
            level=context.backchannel_level,
            language_name=context.persona.display_language,
            phrases=context.persona.voice.backchannel_phrases,
        )
    # Domain-owned callers use the pack's canonical persona continuers.
    return default_backchannel_settings(
        context.language, level=context.backchannel_level
    )


def call_context(config: RunConfig, language_context: LanguageTaskInfo) -> CallContext:
    """The typed per-call context handed to the orchestrator."""
    from tau2.voice.voice_gender import selected_agent_voice

    agent_voice, agent_voice_gender = selected_agent_voice(config)
    return CallContext(
        language=language_context,
        communicate_scoring=config.communicate_scoring,
        agent_voice=agent_voice,
        agent_voice_gender=agent_voice_gender,
    )


def _build_env_kwargs(
    config: RunConfig, task: Task, *, language_context: LanguageTaskInfo
) -> dict:
    """Build env_kwargs from a RunConfig for the environment constructor.

    Passes the resolved language binding and Gaia2 task context.
    """
    from tau2.domains.gaia2.language_runtime import (
        language_environment_kwargs,
        pin_language_artifacts,
    )

    loaded = pin_language_artifacts(config, update=False)
    if loaded.artifact_pins != language_context.artifact_pins:
        raise ValueError("Environment binding differs from the resolved task context")
    env_kwargs: dict = language_environment_kwargs(loaded)
    if registry.get_domain_capabilities(config.domain).get("task_environment"):
        env_kwargs["task"] = task
        env_kwargs["semantic_scoring"] = config.communicate_scoring
        if config.domain == "gaia2" and getattr(
            config, "gaia2_allow_stale_runtime", False
        ):
            env_kwargs["allow_stale_runtime"] = True
    return env_kwargs


def build_text_orchestrator(
    config: TextRunConfig,
    task: Task,
    *,
    seed: Optional[int] = None,
    simulation_id: Optional[str] = None,
    user_persona_config: Optional[PersonaConfig] = None,
    language_context: Optional[LanguageTaskInfo] = None,
) -> Orchestrator:
    """Build a half-duplex (text) orchestrator from a TextRunConfig.

    Args:
        config: Text run configuration.
        task: The task to run.
        seed: Per-trial seed. If None, uses config.seed.
        simulation_id: Unique simulation ID. If None, a UUID is generated.
        user_persona_config: Persona config for the user simulator.
        language_context: Prepared task assignment; current artifact bytes are checked.

    Returns:
        A fully constructed Orchestrator, ready for run_simulation().

    Example:
        config = TextRunConfig(domain="gaia2", agent="llm_agent")
        tasks = get_tasks("gaia2")
        orchestrator = build_text_orchestrator(config, tasks[0], seed=42)
        result = run_simulation(orchestrator)
    """
    if simulation_id is None:
        simulation_id = str(uuid.uuid4())
    if seed is None:
        seed = config.seed

    solo_mode = registry.get_agent_metadata(
        config.effective_agent, "solo_mode", default=False
    )
    from tau2.domains.gaia2.language_runtime import (
        agent_language_guidance,
        task_context,
    )

    language_context = task_context(config, task, context=language_context)
    context = call_context(config, language_context)
    if language_context.persona is not None:
        user_persona_config = language_context.persona
    domain = config.domain
    env_kwargs = _build_env_kwargs(config, task, language_context=language_context)

    environment = build_environment(domain, solo_mode=solo_mode, env_kwargs=env_kwargs)

    hidden_intent_graph = None
    if config.task_set_name == "gaia2_hidden":
        from tau2.data_model.hidden_intent import HiddenGoalGraph
        from tau2.domains.gaia2_hidden.loader import get_goal_graph

        hidden_intent_graph = HiddenGoalGraph.model_validate(get_goal_graph(task.id))

    agent = build_agent(
        config.effective_agent,
        environment,
        language_guidance=agent_language_guidance(
            language_context, agent_voice_gender=context.agent_voice_gender
        ),
        language=language_context.language,
        llm=config.llm_agent,
        llm_args=config.llm_args_agent,
        task=task,
        solo_mode=solo_mode,
    )

    user = build_user(
        config.effective_user,
        environment,
        task,
        llm=config.llm_user,
        llm_args=config.llm_args_user,
        persona_config=user_persona_config,
        solo_mode=solo_mode,
        hidden_intent_graph=hidden_intent_graph,
    )

    orchestrator = Orchestrator(
        domain=domain,
        agent=agent,
        user=user,
        environment=environment,
        task=task,
        max_steps=config.effective_max_steps,
        max_errors=config.max_errors,
        seed=seed,
        solo_mode=solo_mode,
        simulation_id=simulation_id,
        validate_communication=config.enforce_communication_protocol,
        timeout=config.timeout,
        call_context=context,
    )

    logger.debug(
        f"Built text orchestrator: domain={domain}, agent={config.effective_agent}, "
        f"user={config.effective_user}, task={task.id}"
    )

    return orchestrator


def build_voice_orchestrator(
    config: VoiceRunConfig,
    task: Task,
    *,
    seed: Optional[int] = None,
    simulation_id: Optional[str] = None,
    user_voice_settings: Optional[VoiceSettings] = None,
    user_persona_config: Optional[PersonaConfig] = None,
    hallucination_feedback: Optional[str] = None,
    audio_taps_dir: Optional[Path] = None,
    language_context: Optional[LanguageTaskInfo] = None,
) -> FullDuplexOrchestrator:
    """Build a voice trial and clean up prepared resources if construction fails."""
    from tau2.persistent_tasks.availability import reject_retired_task

    reject_retired_task(task)
    with ExitStack() as resources:
        result = _build_voice_orchestrator(
            config,
            task,
            seed=seed,
            simulation_id=simulation_id,
            user_voice_settings=user_voice_settings,
            user_persona_config=user_persona_config,
            hallucination_feedback=hallucination_feedback,
            audio_taps_dir=audio_taps_dir,
            language_context=language_context,
            resources=resources,
        )
        resources.pop_all()  # The constructed trial now owns environment and audio.
        return result


def _build_voice_orchestrator(
    config: VoiceRunConfig,
    task: Task,
    *,
    seed=None,
    simulation_id=None,
    user_voice_settings=None,
    user_persona_config=None,
    hallucination_feedback=None,
    audio_taps_dir=None,
    language_context=None,
    resources,
) -> FullDuplexOrchestrator:
    """Build a full-duplex (voice) orchestrator from a VoiceRunConfig.

    Args:
        config: Voice run configuration.
        task: The task to run.
        seed: Per-trial seed. If None, uses config.seed.
        simulation_id: Unique simulation ID. If None, a UUID is generated.
        user_voice_settings: Pre-computed voice settings (from run-level setup).
            If None, defaults are created.
        user_persona_config: Pre-computed persona config (from run-level setup).
            If None, derived from sampled voice config.
        language_context: Prepared task assignment; current artifact bytes are checked.
        hallucination_feedback: Optional feedback from a previous hallucination
            check. If provided, appended to user instructions to help avoid
            repeating the same errors on retry.

    Returns:
        A fully constructed FullDuplexOrchestrator, ready for run_simulation().

    Raises:
        ValueError: If the agent is registered with solo_mode=True, which is
            not supported for voice/full-duplex runs.

    Example:
        config = VoiceRunConfig(domain="gaia2", audio_native_config=AudioNativeConfig())
        tasks = get_tasks("gaia2")
        orchestrator = build_voice_orchestrator(config, tasks[0], seed=42)
        result = run_simulation(orchestrator)
    """
    if simulation_id is None:
        simulation_id = str(uuid.uuid4())
    if seed is None:
        seed = config.seed

    # Solo mode is not supported for voice/full-duplex runs
    solo_mode = registry.get_agent_metadata(
        config.effective_agent, "solo_mode", default=False
    )
    if solo_mode:
        raise ValueError(
            f"Agent '{config.effective_agent}' is registered with solo_mode=True, "
            f"but solo mode is not supported for voice/full-duplex runs."
        )

    from tau2.domains.gaia2.language_runtime import (
        agent_language_guidance,
        task_context,
    )

    language_context = task_context(config, task, context=language_context)
    context = call_context(config, language_context)
    domain = config.domain
    env_kwargs = _build_env_kwargs(config, task, language_context=language_context)

    environment = build_environment(domain, env_kwargs=env_kwargs)
    close_environment = getattr(environment, "close", None)
    if close_environment is not None:
        resources.callback(close_environment)

    saasr_session = None
    if config.audio_native_config.user_audio_renderer == "saasr":
        from tau2.voice.saasr import SaasrSession, turn_seed

        settings = config.audio_native_config.saasr_config
        source_id = language_context.canonical_task_id
        identity = settings.preflight(domain, [source_id])
        recorded = config.audio_native_config.saasr_identity
        if recorded is not None and (
            recorded["scenes"].get(source_id) != identity["scenes"][source_id]
            or recorded["model_sha256"] != identity["model_sha256"]
            or recorded.get("renderer_sources") != identity.get("renderer_sources")
        ):
            raise ValueError("SAASR resources changed after batch preflight")
        scene = settings.scene_for(domain, source_id)
        directory = (
            audio_taps_dir.parent / "saasr"
            if audio_taps_dir is not None
            else Path("data/results/saasr-artifacts") / simulation_id
        )
        saasr_session = SaasrSession(
            settings,
            scene,
            directory,
            seed=turn_seed(seed, f"{domain}/{source_id}"),
            language={
                "en": "English",
                "zh": "Chinese",
                "ja": "Japanese",
                "ko": "Korean",
                "fr": "French",
                "de": "German",
                "es": "Spanish",
                "pt": "Portuguese",
                "it": "Italian",
                "ru": "Russian",
            }.get(language_context.language.split("-")[0], "Auto"),
            identity=identity,
        )
        resources.callback(saasr_session.close)
        if (
            recorded is not None
            and recorded.get("workers") != saasr_session.pool.identities
        ):
            raise ValueError("SAASR worker software changed after batch preflight")

    if config.audio_native_config.provider == "qwen_agent" and saasr_session is None:
        raise ValueError(
            "qwen_agent requires --user-audio-renderer saasr and --saasr-config"
        )

    observer_hotwords = None
    from tau2.adapters.registry import observer_preparation

    observer_settings = observer_preparation(
        config.audio_native_config.provider,
        config.audio_native_config.qwen_agent_config,
    )
    if observer_settings is not None:
        if observer_settings.hotwords.enabled:
            from tau2.user.asr_hotwords import build_hotword_profile

            prompt_task = task
            hotword_model, hotword_args = config.llm_user, config.llm_args_user
            if observer_settings.api_config_dir:
                from tau2.utils.api_config import get_api_configuration

                hotword_profile = get_api_configuration(
                    observer_settings.api_config_dir
                ).text("ASR_HOTWORDS")
                hotword_model, hotword_args = (
                    hotword_profile.litellm_model,
                    hotword_profile.arguments(),
                )
            try:
                if observer_settings.hotwords.mode == "gaia2_stage_gt":
                    if domain != "gaia2" or language_context.language != "en":
                        raise ValueError("Fixed stage hotwords require English Gaia2")
                    from tau2.user.stage_asr_hotwords import load_stage_hotword_profile

                    observer_hotwords = load_stage_hotword_profile(
                        task, observer_settings.hotwords
                    )
                else:
                    observer_hotwords = build_hotword_profile(
                        prompt_task,
                        environment.get_user_asr_materials(prompt_task),
                        observer_settings.hotwords,
                        language=language_context.language,
                        model=hotword_model,
                        model_args=hotword_args,
                    )
            except Exception as error:
                raise RuntimeError(
                    f"ASR hotword preparation failed ({type(error).__name__})"
                ) from None
            if audio_taps_dir is not None:
                audio_taps_dir.mkdir(parents=True, exist_ok=True)
                vocabulary_path = audio_taps_dir / "observer_hotwords.json"
                vocabulary_path.write_text(
                    json.dumps(observer_hotwords.record(), ensure_ascii=False, indent=2)
                )
                vocabulary_path.chmod(0o600)

    if observer_hotwords is not None:
        agent = build_agent(
            config.effective_agent,
            environment,
            audio_native_config=config.audio_native_config,
            language_guidance=agent_language_guidance(
                language_context, agent_voice_gender=context.agent_voice_gender
            ),
            language=language_context.language,
            audio_taps_dir=audio_taps_dir,
        )
        agent.observer_hotwords = observer_hotwords
    else:
        agent = build_agent(
            config.effective_agent,
            environment,
            audio_native_config=config.audio_native_config,
            language_guidance=agent_language_guidance(
                language_context, agent_voice_gender=context.agent_voice_gender
            ),
            language=language_context.language,
            audio_taps_dir=audio_taps_dir,
        )

    caller_settings = config.audio_native_config.user_simulation
    dynamic_followup_config = (
        caller_settings.dynamic_followup if caller_settings is not None else None
    )
    hidden_intent_graph = None
    if (
        dynamic_followup_config is not None
        and dynamic_followup_config.enabled
        and (domain != "gaia2" or language_context.language != "en")
    ):
        raise ValueError("Dynamic followup v1 requires English Gaia2 voice runs")
    from tau2.domains.gaia2.interaction_variants import variant_for

    if variant_for(task) is not None:
        # Authored changes determine the business content; the generic dynamic
        # followup generator must not replace the temporary or initial request.
        dynamic_followup_config = None
    if config.task_set_name == "gaia2_hidden":
        from tau2.data_model.hidden_intent import HiddenGoalGraph
        from tau2.domains.gaia2_hidden.loader import get_goal_graph

        hidden_intent_graph = HiddenGoalGraph.model_validate(get_goal_graph(task.id))

    user = build_voice_user(
        environment,
        task,
        config.audio_native_config,
        llm=config.llm_user,
        llm_args=config.llm_args_user,
        voice_settings=user_voice_settings,
        persona_config=user_persona_config,
        speech_complexity=config.speech_complexity,
        seed=(
            config.voice_sampling_seed
            if config.voice_sampling_seed is not None
            else seed
            if seed is not None
            else 42
        ),
        domain=domain,
        language_context=language_context,
        hallucination_feedback=hallucination_feedback,
        audio_taps_dir=audio_taps_dir,
        channel_effects_mode=config.channel_effects_mode,
        speech_effects_mode=config.speech_effects_mode,
        force_sample_voice_config=config.pass3_condition is not None,
        saasr_session=saasr_session,
        dynamic_followup_config=dynamic_followup_config,
        hidden_intent_graph=hidden_intent_graph,
    )

    orchestrator = FullDuplexOrchestrator(
        domain=domain,
        agent=agent,
        user=user,
        environment=environment,
        task=task,
        max_steps=config.effective_max_steps,
        max_errors=config.max_errors,
        seed=seed,
        simulation_id=simulation_id,
        tick_duration_seconds=config.audio_native_config.tick_duration_seconds,
        timeout=config.timeout,
        call_context=context,
        watchdog_config=caller_settings.watchdog
        if caller_settings is not None
        else None,
        watchdog_judge=config.communicate_scoring.judge
        if config.communicate_scoring is not None
        else None,
    )
    recheck = getattr(agent, "recheck_heard_audio", None)
    if recheck is not None:
        user.audio_recheck = recheck

    logger.debug(
        f"Built voice orchestrator: domain={domain}, agent={config.effective_agent}, "
        f"user={config.effective_user}, task={task.id}"
    )

    return orchestrator


def build_orchestrator(
    config: RunConfig,
    task: Task,
    *,
    seed: Optional[int] = None,
    simulation_id: Optional[str] = None,
    user_voice_settings: Optional[VoiceSettings] = None,
    user_persona_config: Optional[PersonaConfig] = None,
    hallucination_feedback: Optional[str] = None,
    audio_taps_dir: Optional[Path] = None,
    language_context: Optional[LanguageTaskInfo] = None,
) -> Union[Orchestrator, FullDuplexOrchestrator]:
    """Build a ready-to-run orchestrator from a RunConfig and task.

    Dispatches to build_text_orchestrator or build_voice_orchestrator
    based on the config type.

    Args:
        config: Text or voice run configuration.
        task: The task to run.
        seed: Per-trial seed. If None, uses config.seed.
        simulation_id: Unique simulation ID. If None, a UUID is generated.
        user_voice_settings: Pre-computed voice settings (voice mode only).
        user_persona_config: Pre-computed persona config.
        language_context: Prepared language/domain/task/persona resolution.
        hallucination_feedback: Optional feedback from a previous hallucination
            check (voice mode only). Passed through to build_voice_orchestrator.

    Returns:
        A fully constructed Orchestrator or FullDuplexOrchestrator.
    """
    if isinstance(config, VoiceRunConfig):
        return build_voice_orchestrator(
            config,
            task,
            seed=seed,
            simulation_id=simulation_id,
            language_context=language_context,
            user_voice_settings=user_voice_settings,
            user_persona_config=user_persona_config,
            hallucination_feedback=hallucination_feedback,
            audio_taps_dir=audio_taps_dir,
        )
    else:
        return build_text_orchestrator(
            config,
            task,
            seed=seed,
            simulation_id=simulation_id,
            language_context=language_context,
            user_persona_config=user_persona_config,
        )

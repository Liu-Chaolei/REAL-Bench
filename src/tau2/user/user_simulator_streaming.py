import hashlib
import json
import random
import re
import time
import uuid
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from loguru import logger
from pydantic import Field

from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.agent.base.streaming import (
    AudioChunkingMixin,
    ListenerReactionDecision,
    StreamingState,
    TurnTakingAction,
    basic_turn_taking_policy,
)
from tau2.agent.base.voice import VoiceMixin, VoiceState
from tau2.backchannel import BackchannelSettings, default_backchannel_settings
from tau2.config import (
    VOICE_USER_SIMULATOR_DECISION_MODEL,
    VOICE_USER_SIMULATOR_DECISION_MODEL_ARGS,
    VOICE_USER_SIMULATOR_VERSION,
)
from tau2.data_model.audio import (
    PCM_SAMPLE_RATE,
    AudioData,
    AudioEncoding,
    audio_bytes_to_string,
)
from tau2.data_model.audio_effects import (
    ChannelEffectsResult,
    EffectTimeline,
    SpellOutDetection,
)
from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.data_model.hidden_intent import HiddenGoalGraph
from tau2.data_model.message import (
    AssistantMessage,
    EnvironmentMessage,
    Message,
    SystemMessage,
    ToolCall,
    UserMessage,
)
from tau2.data_model.persona import InterruptTendency, PersonaConfig
from tau2.data_model.transcript import LinearizationStrategy
from tau2.data_model.voice import VoiceSettings
from tau2.environment.tool import Tool
from tau2.user.async_voice import PendingVoiceTask
from tau2.user.dynamic_followup import (
    classify_status_affect,
    generate_dynamic_followup,
    infer_status_affect,
)
from tau2.user.guidelines import simulation_guidelines
from tau2.user.hidden_intent import HIDDEN_CONTEXT_PREFIX, HiddenIntentRuntime
from tau2.user.user_simulator import UserSimulator, build_user_system_prompt
from tau2.user.user_simulator_base import (
    OUT_OF_SCOPE,
    STOP,
    TRANSFER,
    FullDuplexVoiceUser,
    UserState,
    ValidUserInputMessage,
)
from tau2.utils.llm_utils import generate
from tau2.utils.transcript.linearization import merge_homogeneous_chunks
from tau2.utils.utils import get_now
from tau2.voice.interaction_inserts import (
    InteractionInsertEngine,
    PendingInteractionInsert,
)
from tau2.voice.synthesis.audio_effects.effects import StreamingTelephonyConverter
from tau2.voice.synthesis.audio_effects.processor import (
    PendingEffectState,
    StreamingAudioEffectsMixin,
)
from tau2.voice.synthesis.audio_effects.scheduler import EffectScheduler
from tau2.voice.synthesis.audio_effects.speech_generator import (
    OutOfTurnSpeechGenerator,
    create_streaming_audio_generators,
)
from tau2.voice.utils.audio_tap import AudioTap
from tau2.voice_config import (
    resolve_background_noise_path,
    resolve_burst_noise_paths,
)

# Prompt for interruption decision.
# Note: Experimented with multiple sentences in the "Consider" section on 3 tasks. Below setting was best.
# 2nd best for first 2 sentences only (out of the 4 sentences), which would be less explicit guidance on when to interrupt.
INTERRUPTION_DECISION_PROMPT = """You are analyzing a conversation to decide if the user should interrupt the agent.

Conversation history (most recent at bottom):

<conversation_history>
{conversation_history}
</conversation_history>

The agent is CURRENTLY speaking (you can see their ongoing speech in the conversation above).

Based on the conversation so far, should the user interrupt the agent NOW?

Consider:
- Has the user heard enough to understand what the agent is asking or saying?
- Has the user heard enough to have a response, question, or correction ready?
- Did the agent just complete the sentence which has all the pertinent information the user was looking for?
- Do NOT repeatedly interrupt the agent if it has spoken only a few words (say less than 5 words).

Respond with ONLY "YES" if the user should interrupt now, or "NO" if they should keep listening.
"""
# - Is the agent currently in the middle of a sentence which seems to be "filler" sentence with not much additional information?


class UserStreamingState(UserState, StreamingState[ValidUserInputMessage, UserMessage]):
    """
    State for user streaming.
    Extends UserState, StreamingState, and VoiceState with streaming-specific fields.
    """


class UserAudioStreamingState(
    UserState, StreamingState[ValidUserInputMessage, UserMessage], VoiceState
):
    """
    State for user audio streaming.
    Extends UserState, StreamingState, and VoiceState with audio streaming-specific fields.
    """

    model_config = {"arbitrary_types_allowed": True}

    user_utterance_count: int = 0
    agent_input_revision: int = 0
    agent_speech_epoch: int = 0
    agent_speech_active: bool = False
    agent_silent_ticks: int = 0
    agent_transcript_contents: dict[str, str] = Field(default_factory=dict)
    elapsed_samples: int = 0
    effect_scheduler: Optional[EffectScheduler] = None
    out_of_turn_speech_generator: Optional[OutOfTurnSpeechGenerator] = None

    pending_effect: Optional[PendingEffectState] = None

    interaction_insert: Optional[PendingInteractionInsert] = None

    telephony_converter: Optional[StreamingTelephonyConverter] = None

    @property
    def info(self) -> str:
        """
        Get information about the state.
        """
        return (
            super().info
            + f", User utterance count: {self.user_utterance_count}, Elapsed samples: {self.elapsed_samples}"
        )


def user_interruption_policy(
    state: UserStreamingState,
    integration_ticks: int = 1,
    decision_model: Optional[str] = None,
    decision_model_args: Optional[dict] = None,
) -> ListenerReactionDecision:
    """
    Decide whether the user should interrupt the agent while they're speaking.

    This is called when the agent is currently speaking and the user is listening.
    It determines if the user has heard enough to formulate a response and wants to interrupt.

    This function is reusable by both text and voice streaming user simulators.

    Args:
        state: The current streaming state
        backchannel: The call's density level and continuers, rendered into
            the shared decision template.
        integration_ticks: Number of consecutive silent ticks before an overlap region ends
            during linearization. Higher values are more tolerant of brief pauses. Default is 1.

    Returns:
        ListenerReactionDecision with decision and metadata from the LLM call
    """
    # Build the conversation history from tick-based history (including current pending input)
    linearized_messages = state.get_linearized_messages(
        strategy=LinearizationStrategy.CONTAINMENT_AWARE,
        include_pending_input=True,
        indicate_current_incomplete=True,
        integration_ticks=integration_ticks,
    )

    if not linearized_messages and not state.input_turn_taking_buffer:
        return ListenerReactionDecision(decision=False)

    # Format conversation history
    formatted_history = _format_conversation_history(linearized_messages)

    logger.info(f"CHECKING INTERRUPTION:\nSent to LLM:\n{formatted_history}\n\n\n")

    # Build the prompt for interruption decision using template
    interruption_prompt = INTERRUPTION_DECISION_PROMPT.format(
        conversation_history=formatted_history
    )

    # Create messages for LLM call
    decision_messages = [UserMessage(role="user", content=interruption_prompt)]

    try:
        response = generate(
            model=decision_model or VOICE_USER_SIMULATOR_DECISION_MODEL,
            messages=decision_messages,
            call_name="interruption_decision",
            **(
                VOICE_USER_SIMULATOR_DECISION_MODEL_ARGS
                if decision_model_args is None
                else decision_model_args
            ),
        )

        decision_text = response.content.strip().upper()
        should_interrupt = decision_text == "YES"

        logger.debug(f"Interruption decision: {decision_text}")

        return ListenerReactionDecision(
            decision=should_interrupt,
            generation_time_seconds=response.generation_time_seconds,
            cost=response.cost,
            usage=response.usage,
        )
    except Exception as e:
        logger.error(f"Error in interruption decision: {e}")
        # Default to not interrupting on error
        return ListenerReactionDecision(decision=False)


def user_backchannel_policy(
    state: UserStreamingState,
    backchannel: BackchannelSettings,
    integration_ticks: int = 1,
    decision_model: Optional[str] = None,
    decision_model_args: Optional[dict] = None,
) -> ListenerReactionDecision:
    """
    Decide whether the user should backchannel while the agent is speaking.

    This is called when the agent is currently speaking and the user is listening.
    It determines if the user should emit a brief acknowledgment (e.g., "uh-huh", "ok").

    This function is reusable by both text and voice streaming user simulators.

    Args:
        state: The current streaming state
        backchannel: The call's density level and continuers, rendered into
            the shared decision template.
        integration_ticks: Number of consecutive silent ticks before an overlap region ends
            during linearization. Higher values are more tolerant of brief pauses. Default is 1.

    Returns:
        ListenerReactionDecision with decision and metadata from the LLM call
    """
    # Build the conversation history from tick-based history (including current pending input)
    linearized_messages = state.get_linearized_messages(
        strategy=LinearizationStrategy.CONTAINMENT_AWARE,
        include_pending_input=True,
        indicate_current_incomplete=True,
        integration_ticks=integration_ticks,
    )

    if not linearized_messages and not state.input_turn_taking_buffer:
        return ListenerReactionDecision(decision=False)

    # Format conversation history
    formatted_history = _format_conversation_history(linearized_messages)

    logger.info(f"CHECKING BACKCHANNEL:\nSent to LLM:\n{formatted_history}\n\n\n")

    # Build the prompt for backchannel decision using template
    decision_prompt = backchannel.decision_prompt(formatted_history)

    # Create messages for LLM call
    decision_messages = [UserMessage(role="user", content=decision_prompt)]

    try:
        response = generate(
            model=decision_model or VOICE_USER_SIMULATOR_DECISION_MODEL,
            messages=decision_messages,
            call_name="backchannel_decision",
            **(
                VOICE_USER_SIMULATOR_DECISION_MODEL_ARGS
                if decision_model_args is None
                else decision_model_args
            ),
        )

        decision_text = response.content.strip().upper()
        should_backchannel = decision_text == "YES"

        logger.debug(f"Backchannel decision: {decision_text}")

        return ListenerReactionDecision(
            decision=should_backchannel,
            generation_time_seconds=response.generation_time_seconds,
            cost=response.cost,
            usage=response.usage,
        )
    except Exception as e:
        logger.error(f"Error in backchannel decision: {e}")
        # Default to not backchanneling on error
        return ListenerReactionDecision(decision=False)


class UserAudioStreamingMixin(
    AudioChunkingMixin[ValidUserInputMessage, UserMessage, UserAudioStreamingState]
):
    """
    Agent-specific audio chunking mixin.
    This is a specialization of AudioChunkingMixin for agents with audio-based chunking.
    """


class VoiceStreamingUserSimulator(
    VoiceMixin[ValidUserInputMessage, UserMessage, UserAudioStreamingState],
    StreamingAudioEffectsMixin,
    UserAudioStreamingMixin,
    LLMConfigMixin,
    FullDuplexVoiceUser[UserAudioStreamingState],
):
    """
    Full-duplex LLM-based user simulator with voice-based streaming.

    Inherits from:
    - VoiceMixin: Provides voice logic (TTS, STT, background noise)
    - UserAudioStreamingMixin: Provides audio chunking and streaming logic
    - LLMConfigMixin: Provides LLM configuration (llm, llm_args, set_seed)
    - FullDuplexVoiceUser: Provides full-duplex streaming + voice interface

    Features:
    - Enhanced tool call handling (tool calls are never chunked)
    - Custom turn-taking logic (responds immediately by default)
    - Proper state typing with UserAudioStreamingState

    Usage:
        user = VoiceStreamingUserSimulator(
            tools=tools,
            domain_policy=policy,
            llm="gpt-4",
            chunk_size=10,
        )

        # FULL_DUPLEX mode
        state = user.get_init_state()  # Returns UserAudioStreamingState
        chunk, state = user.get_next_chunk(state, incoming_chunk)
    """

    def __init__(
        self,
        tools: List[Tool],
        instructions: str,
        llm: Optional[str] = None,
        llm_args: Optional[dict] = None,
        chunk_size: int = 10,
        voice_settings: VoiceSettings = VoiceSettings(),
        wait_to_respond_threshold_other: int = 2,
        wait_to_respond_threshold_self: int = 4,
        yield_threshold_when_interrupted: Optional[int] = 2,
        yield_threshold_when_interrupting: Optional[int] = None,
        backchannel_min_threshold: Optional[int] = None,
        backchannel_max_threshold: Optional[int] = None,
        backchannel_poisson_rate: Optional[float] = None,
        use_llm_backchannel: bool = True,
        interruption_check_interval: Optional[int] = None,
        integration_ticks: int = 1,
        silence_annotation_threshold_ticks: Optional[int] = None,
        tick_duration_seconds: float = 0.05,
        persona_config: Optional[PersonaConfig] = None,
        audio_taps_dir: Optional["Path"] = None,
        realtime_generation: bool = False,
        global_guidelines_override: Optional[str] = None,
        role_reminder_override: Optional[str] = None,
        spell_event_detector: Optional[Callable[[str], list[SpellOutDetection]]] = None,
        backchannel: Optional[BackchannelSettings] = None,
        decision_model: Optional[str] = None,
        decision_model_args: Optional[dict] = None,
        silence_checkin_threshold: Optional[int] = None,
        saasr_session=None,
        interaction_insert_config=None,
        dynamic_followup_config: DynamicFollowupConfig | None = None,
        hidden_intent_graph: HiddenGoalGraph | None = None,
        api_config_dir: str | None = None,
    ):
        """
        Initialize the streaming user simulator.

        Args:
            tools: List of available tools
            instructions: The instructions for the user.
            llm: LLM model name
            llm_args: Additional LLM arguments
            chunk_size: Number of units per chunk
            wait_to_respond_threshold_other: Minimum time to wait since OTHER (agent) last spoke before generating a response.
                Both this AND wait_to_respond_threshold_self must be satisfied.
            wait_to_respond_threshold_self: Minimum time to wait since SELF (user) last spoke before generating a response.
                Both this AND wait_to_respond_threshold_other must be satisfied.
            yield_threshold_when_interrupted: How long user keeps speaking when agent interrupts user. If None, cannot be interrupted.
            yield_threshold_when_interrupting: How long user keeps speaking when user interrupts agent. If None, uses yield_threshold_when_interrupted.
            backchannel_min_threshold: Min threshold for backchanneling (ticks). If None and using Poisson, cannot backchannel.
            backchannel_max_threshold: Max threshold for backchanneling (ticks). Used with Poisson policy.
            backchannel_poisson_rate: Poisson rate for backchanneling (events/second). Used with Poisson policy.
            use_llm_backchannel: If True, use LLM-based backchannel policy. If False, use Poisson-based policy.
            interruption_check_interval: If set, only check for interruption every N ticks. Useful to reduce callback frequency.
            integration_ticks: Number of consecutive silent ticks before an overlap region ends
                during linearization. Higher values are more tolerant of brief pauses. Default is 1.
            silence_annotation_threshold_ticks: If set, add silence annotations to conversation history
                when both parties are silent for more than this many ticks.
            tick_duration_seconds: Duration of each tick in seconds. Used for backchanneling Poisson calculations.
            voice_settings: Voice settings for the user.
            persona_config: Runtime persona configuration for user behavior (e.g., verbosity level, interrupt tendency)
            backchannel: Density level and continuers of the call language; None
                uses the canonical English pack's.
            audio_taps_dir: If set, record audio at each pipeline stage to WAV files in this directory.
            silence_checkin_threshold: Minimum silent ticks before an unsolicited
                check-in. Fresh input, initial speech and tool replies bypass this gate.
        """
        # Initialize mixin and base class
        super().__init__(
            tools=tools,
            instructions=instructions,
            llm=llm,
            llm_args=llm_args,
            chunk_size=chunk_size,
            voice_settings=voice_settings,
        )
        self.persona_config = persona_config or PersonaConfig()
        self.backchannel = backchannel or default_backchannel_settings()
        self.api_config_dir = api_config_dir
        self.decision_model = decision_model
        self.decision_model_args = decision_model_args
        self.global_guidelines_override = global_guidelines_override
        self.role_reminder_override = role_reminder_override
        self.spell_event_detector = spell_event_detector
        self.integration_ticks = integration_ticks
        self.silence_annotation_threshold_ticks = silence_annotation_threshold_ticks
        self.tick_duration_seconds = tick_duration_seconds
        self.validate_voice_settings()
        self.wait_to_respond_threshold_other = wait_to_respond_threshold_other
        self.wait_to_respond_threshold_self = wait_to_respond_threshold_self
        self.silence_checkin_threshold = silence_checkin_threshold
        self.yield_threshold_when_interrupted = yield_threshold_when_interrupted
        self.yield_threshold_when_interrupting = yield_threshold_when_interrupting
        self.backchannel_min_threshold = backchannel_min_threshold
        self.backchannel_max_threshold = backchannel_max_threshold
        self.backchannel_poisson_rate = backchannel_poisson_rate
        self.use_llm_backchannel = use_llm_backchannel
        self.interruption_check_interval = interruption_check_interval
        self.realtime_generation = realtime_generation
        self.saasr_session = saasr_session
        self.interaction_insert_engine = None
        if saasr_session is not None and interaction_insert_config is not None:
            speech_environment = self.voice_settings.speech_environment
            insert_provider_config = interaction_insert_config.provider_config
            self.interaction_insert_engine = InteractionInsertEngine(
                synthesis_config=interaction_insert_config,
                speech_config=interaction_insert_config.speech_effects_config,
                persona_name=getattr(speech_environment, "persona_name", "default"),
                voice_id=getattr(insert_provider_config, "voice_id", None),
                sample_rate=PCM_SAMPLE_RATE,
                seed=getattr(speech_environment, "voice_seed", 42),
            )
        self.dynamic_followup_config = dynamic_followup_config
        self.hidden_intent_runtime = (
            HiddenIntentRuntime(hidden_intent_graph)
            if hidden_intent_graph is not None
            else None
        )
        self._hidden_intent_events: list[dict] = []
        self._hidden_intent_seen_questions: set[str] = set()
        self.scenario_dynamic_stage_ids: dict[str, int] = {}
        self.scenario_dynamic_stage_tool_gts: dict[str, list[dict]] = {}
        self._dynamic_followup_events: list[dict] = []
        self._dynamic_followup_cache: dict[str, dict] = {}
        if saasr_session is not None and not realtime_generation:
            raise ValueError("SAASR needs asynchronous user generation")
        self._scenario_inputs = []
        self.scenario_constraints = None
        self._released_scenario_facts = []
        self._candidate_recovery_events = []
        from tau2.config import DEFAULT_USER_CANDIDATE_RETRIES

        self.candidate_max_retries = DEFAULT_USER_CANDIDATE_RETRIES
        self.audio_recheck = None
        self._generation_closed = False
        self._generation_task: PendingVoiceTask | None = None
        self._listener_task: PendingVoiceTask | None = None
        self._needs_tool_reply = False
        self._voice_task_sequence = 0
        self._generation_events: list[dict] = []
        self._additional_llm_cost: float | None = 0.0
        self._latest_state: UserAudioStreamingState | None = None
        self._last_generation_tick = 0
        self._user_executor = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-user")
            if realtime_generation
            else None
        )
        self._saasr_stream = None

        # Default yield_threshold_when_interrupting to yield_threshold_when_interrupted if not set
        if (
            self.yield_threshold_when_interrupting is None
            and self.yield_threshold_when_interrupted is not None
        ):
            self.yield_threshold_when_interrupting = (
                self.yield_threshold_when_interrupted
            )
            logger.info(
                f"yield_threshold_when_interrupting not set, defaulting to yield_threshold_when_interrupted={self.yield_threshold_when_interrupted}"
            )
        # Enable user-initiated interruption based on persona config
        if persona_config is not None and persona_config.interrupt_tendency is not None:
            # Enable user-initiated interruption for users with INTERRUPTS tendency
            self.enable_user_initiated_interruption = (
                persona_config.interrupt_tendency == InterruptTendency.INTERRUPTS
            )
        else:
            # No persona config or interrupt_tendency is None
            self.enable_user_initiated_interruption = False
        self.validate_turn_taking_settings()

        # Effect timeline (always active, lightweight metadata)
        self._effect_timeline = EffectTimeline()

        # Audio taps for pipeline diagnostics (None = disabled, zero cost)
        self._audio_taps: Optional[dict[str, AudioTap]] = None
        if audio_taps_dir is not None:
            from tau2.data_model.audio import TELEPHONY_SAMPLE_RATE

            user_output_sample_rate = (
                PCM_SAMPLE_RATE if saasr_session is not None else TELEPHONY_SAMPLE_RATE
            )
            user_output_encoding = (
                AudioEncoding.PCM_S16LE
                if saasr_session is not None
                else AudioEncoding.ULAW
            )

            self._audio_taps = {
                # Pipeline-stage taps (existing)
                "agent_input": AudioTap(
                    "user_agent-input",
                    audio_taps_dir,
                    TELEPHONY_SAMPLE_RATE,
                    AudioEncoding.ULAW,
                ),
                "tts_output": AudioTap(
                    "user_tts-output",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
                "post_noise": AudioTap(
                    "user_post-noise",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
                "post_telephony": AudioTap(
                    "user_post-telephony",
                    audio_taps_dir,
                    TELEPHONY_SAMPLE_RATE,
                    AudioEncoding.ULAW,
                ),
                "user_output": AudioTap(
                    "user_output",
                    audio_taps_dir,
                    user_output_sample_rate,
                    user_output_encoding,
                ),
                # Per-effect multitrack taps (time-aligned, same length)
                "speech_only": AudioTap(
                    "user_speech-only",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
                "background_noise_only": AudioTap(
                    "user_background-noise-only",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
                "burst_noise_only": AudioTap(
                    "user_burst-noise-only",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
                "out_of_turn_speech_only": AudioTap(
                    "user_out-of-turn-speech-only",
                    audio_taps_dir,
                    PCM_SAMPLE_RATE,
                    AudioEncoding.PCM_S16LE,
                ),
            }
            logger.info(f"Audio taps enabled, output dir: {audio_taps_dir}")

    def validate_turn_taking_settings(self) -> None:
        """Validate the turn-taking settings."""
        if (
            self.yield_threshold_when_interrupted is not None
            and self.yield_threshold_when_interrupted < 2
        ):
            raise ValueError(
                f"yield_threshold_when_interrupted must be at least 2. Got {self.yield_threshold_when_interrupted}. Setting it lower will result in unstable behavior."
            )
        if (
            self.yield_threshold_when_interrupting is not None
            and self.yield_threshold_when_interrupting < 2
        ):
            raise ValueError(
                f"yield_threshold_when_interrupting must be at least 2. Got {self.yield_threshold_when_interrupting}. Setting it lower will result in unstable behavior."
            )

    def validate_voice_settings(self) -> None:
        """Validate the voice settings."""
        if self.voice_settings is None:
            raise ValueError("Voice settings must be provided")
        if self.voice_settings.transcription_enabled:
            raise ValueError("Transcription is not supported for user yet")
        if not self.voice_settings.synthesis_enabled:
            raise ValueError("Voice synthesis must be enabled for user simulator")

    @property
    def global_simulation_guidelines(self) -> str:
        """The voice-specific simulation guidelines for the user simulator."""
        return simulation_guidelines(
            self.persona_config,
            mode="voice",
            use_tools=self.tools is not None,
            override=self.global_guidelines_override,
        )

    @property
    def system_prompt(self) -> str:
        """The complete caller prompt, including persona and task instructions."""
        if self.instructions is None:
            logger.warning("No instructions provided for user simulator")
        return build_user_system_prompt(
            self.instructions,
            persona_config=self.persona_config,
            mode="voice",
            guidelines=self.global_simulation_guidelines,
        )

    def _initial_system_messages(self) -> list[SystemMessage]:
        """Build private user-side system messages for a fresh call."""
        messages = [SystemMessage(role="system", content=self.system_prompt)]
        if self.hidden_intent_runtime is not None:
            messages.append(
                SystemMessage(
                    role="system", content=self.hidden_intent_runtime.user_context()
                )
            )
        return messages

    def _sync_hidden_intent_context(self, state: UserAudioStreamingState) -> None:
        """Refresh the private user-side state message after a transition."""
        if self.hidden_intent_runtime is None:
            return
        context = SystemMessage(
            role="system", content=self.hidden_intent_runtime.user_context()
        )
        for index, system_message in enumerate(state.system_messages):
            if system_message.content and system_message.content.startswith(
                HIDDEN_CONTEXT_PREFIX
            ):
                state.system_messages[index] = context
                return
        state.system_messages.append(context)

    def _api_settings(self, role, default_model=None, default_args=None):
        """Select an optional caller sub-role from the shared startup snapshot."""
        if self.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            profile = get_api_configuration(self.api_config_dir).text(role)
            return profile.litellm_model, profile.arguments()
        return (
            default_model or self.llm,
            self.llm_args if default_args is None else default_args,
        )

    def _observe_hidden_question(
        self, question: str, state: UserAudioStreamingState
    ) -> None:
        """Advance hidden state once for a newly observed Agent turn."""
        if self.hidden_intent_runtime is None or not question:
            return
        key = " ".join(question.split())
        if key in self._hidden_intent_seen_questions:
            return
        self._hidden_intent_seen_questions.add(key)
        model, args = self._api_settings("HIDDEN_INTENT")
        result = self.hidden_intent_runtime.detect_and_apply(
            question, model=model, model_args=args
        )
        self._hidden_intent_events.append(
            {
                "question": question,
                **result,
                "revision": self.hidden_intent_runtime.state.revision,
            }
        )
        if result.get("released"):
            self._sync_hidden_intent_context(state)
            self._sync_released_hidden_facts()

    def _sync_released_hidden_facts(self) -> None:
        """Expose released values to the existing candidate grounding context."""
        if self.hidden_intent_runtime is None:
            return
        for fact in self.hidden_intent_runtime.released_facts():
            if fact not in self._released_scenario_facts:
                self._released_scenario_facts.append(fact)

    @classmethod
    def is_stop(cls, message: Message) -> bool:
        """Check if the message is a stop message."""
        if message.is_tool_call():
            return False
        # Audio-only messages (chunks) don't have text content
        if message.content is None:
            return False
        return (
            STOP in message.content
            or TRANSFER in message.content
            or OUT_OF_SCOPE in message.content
        )

    def get_next_chunk(
        self,
        state: "UserAudioStreamingState",
        participant_chunk: AssistantMessage,
        tool_results: Optional[EnvironmentMessage] = None,
    ) -> Tuple[UserMessage, "UserAudioStreamingState"]:
        """Process one tick, recording incoming agent audio if taps are enabled."""
        self._latest_state = state
        if self.realtime_generation:
            self._observe_agent_input(state, participant_chunk)
        if self._audio_taps:
            self._audio_taps["agent_input"].record_message(participant_chunk)
        message, state = super().get_next_chunk(state, participant_chunk, tool_results)
        self._discard_last_control_message_from_history(state, message)
        return message, state

    def _discard_last_control_message_from_history(
        self, state: "UserAudioStreamingState", message: UserMessage
    ) -> None:
        """Keep control signals out of the private user transcript history.

        Intermediate ``###STOP###`` messages are consumed by the scenario-stage
        controller. They are returned to the orchestrator unchanged, but they
        must not become a user transcript chunk immediately before the next
        stage's synthesized audio. The latter would make the linearizer try to
        merge text and audio messages into one chunk.
        """
        if not self.is_stop(message) or not state.ticks:
            return
        last_tick = state.ticks[-1]
        if last_tick.self_chunk is None or not self.is_stop(last_tick.self_chunk):
            return
        state.ticks[-1] = last_tick.model_copy(update={"self_chunk": None})

    def cancel_pending_generation(self) -> None:
        """Close generation admission before the Agent and ASR are stopped."""
        self._generation_closed = True
        if self.saasr_session is not None:
            self.saasr_session.cancelled.set()

    def stop(
        self,
        participant_chunk: Optional[Message] = None,
        state: Optional["UserAudioStreamingState"] = None,
        tool_results: Optional[EnvironmentMessage] = None,
    ) -> None:
        """Stop the user simulator, close timeline events, and save audio taps."""
        self.cancel_pending_generation()
        state = state or self._latest_state
        try:
            if self._user_executor is not None:
                if state is not None:
                    for task in (self._generation_task, self._listener_task):
                        if task is not None:
                            self._discard_task(task, state, "simulation_ended")
                self._user_executor.shutdown(wait=True, cancel_futures=True)
                if state is not None:
                    self._refresh_voice_tasks(state)
        finally:
            if self._saasr_stream is not None:
                self._saasr_stream.close()
                self._saasr_stream = None
            if self.saasr_session is not None:
                self.saasr_session.close()
            # Persist recordings even when an unplayed request failed while
            # the other participant was ending the call.
            if state is not None:
                end_ms = int(state.elapsed_samples * 1000 / PCM_SAMPLE_RATE)
                self._effect_timeline.close_all_open(end_ms)
            if self._audio_taps:
                for tap in self._audio_taps.values():
                    tap.save()
                logger.info(f"Saved {len(self._audio_taps)} audio taps")
            super().stop(participant_chunk, state, tool_results)

    def get_effect_timeline(self) -> EffectTimeline:
        """Return the effect timeline recorded during the simulation."""
        return self._effect_timeline

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> UserAudioStreamingState:
        """
        Get the initial state of the streaming user.

        Args:
            message_history: The message history of the conversation.

        Returns:
            The initial state of the streaming user (UserAudioStreamingState).
        """
        if message_history is None:
            message_history = []

        synthesis_config = self.voice_settings.synthesis_config
        speech_env = self.voice_settings.speech_environment

        if self.saasr_session is not None:
            return UserAudioStreamingState(
                system_messages=self._initial_system_messages(),
                messages=message_history,
                noise_generator=None,
                effect_scheduler=None,
                out_of_turn_speech_generator=None,
                pending_effect=None,
                interaction_insert=None,
                telephony_converter=None,
                backchannel_rng=random.Random(speech_env.voice_seed + 100),
            )

        # Resolve filenames to full paths
        background_noise_file = resolve_background_noise_path(
            speech_env.background_noise_file
        )
        burst_noise_files = (
            resolve_burst_noise_paths(speech_env.burst_noise_files)
            if speech_env.burst_noise_files
            else None
        )

        noise_generator, out_of_turn_speech_generator = (
            create_streaming_audio_generators(
                synthesis_config=synthesis_config,
                persona_name=speech_env.persona_name,
                voice_id=speech_env.voice_id,
                sample_rate=PCM_SAMPLE_RATE,
                background_noise_file=background_noise_file,
            )
        )

        # Use effect configs directly - complexity overrides already merged in run.py
        effect_scheduler = EffectScheduler(
            seed=speech_env.voice_seed,
            source_config=synthesis_config.source_effects_config,
            speech_config=synthesis_config.speech_effects_config,
            channel_config=synthesis_config.channel_effects_config,
            sample_rate=PCM_SAMPLE_RATE,
            burst_noise_files=burst_noise_files,
            # No override params needed - configs already have complexity values merged
        )

        # Create streaming telephony converter (preserves filter state between chunks)
        telephony_converter = (
            StreamingTelephonyConverter(input_sample_rate=PCM_SAMPLE_RATE)
            if speech_env.telephony_enabled
            else None
        )

        # Create streaming state
        backchannel_rng = random.Random(speech_env.voice_seed + 100)
        return UserAudioStreamingState(
            system_messages=self._initial_system_messages(),
            messages=message_history,
            noise_generator=noise_generator,
            effect_scheduler=effect_scheduler,
            out_of_turn_speech_generator=out_of_turn_speech_generator,
            elapsed_samples=0,
            input_turn_taking_buffer=[],
            output_streaming_queue=[],
            pending_background_noise_chunks=[],
            pending_burst_noise_chunks=[],
            telephony_converter=telephony_converter,
            backchannel_rng=backchannel_rng,
        )

    def speech_detection(self, chunk: Optional[ValidUserInputMessage]) -> bool:
        """
        Check if the chunk is a speech chunk.
        """
        if not isinstance(chunk, AssistantMessage):
            return False
        # Check contains_speech flag - defaults to True for backward compatibility if not set
        from tau2.voice.audio_activity import is_speaking

        return is_speaking(chunk)

    @property
    def _generation(self) -> Future | None:
        return self._generation_task.future if self._generation_task else None

    def _observe_agent_input(self, state, chunk) -> None:
        """Version only received speech/text, never speculative Gateway captions."""
        speech = self.speech_detection(chunk)
        if speech:
            if not state.agent_speech_active:
                state.agent_speech_epoch += 1
            state.agent_speech_active = True
            state.agent_silent_ticks = 0
        else:
            state.agent_silent_ticks += 1
            if state.agent_silent_ticks >= max(1, self.integration_ticks):
                state.agent_speech_active = False
        text = bool(chunk.content and chunk.content.strip())
        fragments = ((chunk.raw_data or {}).get("live_transcript") or {}).get(
            "fragments", []
        )
        identified = [f for f in fragments if f.get("event_id")]
        if identified:
            text = False
            for fragment in identified:
                event_id = fragment["event_id"]
                content = fragment.get("text", chunk.content or "")
                if state.agent_transcript_contents.get(event_id) != content:
                    text = True
                    state.agent_transcript_contents[event_id] = content
        if speech or text:
            state.agent_input_revision += 1

    def _voice_event(self, event, task, state, **details) -> None:
        record = {
            "event": event,
            "id": task.id,
            "origin": task.origin,
            "tick": state.tick_count,
            "time": get_now(),
            "monotonic": time.monotonic(),
            "snapshot_revision": task.revision,
            "input_revision": state.agent_input_revision,
            "speech_epoch": state.agent_speech_epoch,
            "agent_speaking": state.agent_speech_active,
            **details,
        }
        self._generation_events.append(record)
        logger.info("USER_GENERATION_EVENT {}", json.dumps(record))

    def _new_voice_task(
        self, future, origin, action, state, rng=None
    ) -> PendingVoiceTask:
        self._voice_task_sequence += 1
        task = PendingVoiceTask(
            id=self._voice_task_sequence,
            future=future,
            origin=origin,
            revision=state.agent_input_revision,
            speech_epoch=state.agent_speech_epoch,
            started_during_speech=state.agent_speech_active,
            started_tick=state.tick_count,
            input_count=len(state.input_turn_taking_buffer),
            decision=action.model_copy(deep=True),
            rng=rng,
            from_tool_result=self._needs_tool_reply,
        )
        self._voice_event("started", task, state)
        return task

    @staticmethod
    def _invalid_voice_task(task, state) -> str | None:
        if task.origin == "scripted":
            return None  # A released script stays valid; playback waits for silence.
        if task.origin in {"normal", "tool_result"}:
            if state.agent_input_revision != task.revision:
                return "new_agent_input"
            if state.agent_speech_active:
                return "agent_speech_in_progress"
        elif task.started_during_speech:
            # Deliberate reactions may use a partial reply, but only the same
            # speech episode that originally authorized the reaction.
            if (
                not state.agent_speech_active
                or state.agent_speech_epoch != task.speech_epoch
            ):
                return "agent_speech_ended_or_replaced"
        elif state.agent_input_revision != task.revision:
            return "new_agent_input"
        return None

    def _discard_task(self, task, state, reason) -> None:
        if task.discard_reason is None:
            task.discard_reason = reason
            self._voice_event("invalidated", task, state, reason=reason)
        # Future.cancel cannot interrupt a running HTTP request. Keep its
        # handle until it settles, without queueing replacement requests.
        task.future.cancel()

    def _add_unplayed_cost(self, cost) -> None:
        if cost is None:
            self._additional_llm_cost = None
        elif self._additional_llm_cost is not None:
            self._additional_llm_cost += cost

    def _record_generated_result(self, task, message, generated, state, discarded):
        self._voice_event(
            "settled",
            task,
            state,
            discarded=discarded,
            reason=task.discard_reason,
            llm_cost=message.cost,
            llm_usage=message.usage,
            llm_seconds=getattr(generated, "_llm_generation_seconds", None),
            tts_seconds=getattr(generated, "_tts_synthesis_seconds", None),
        )
        if discarded:
            self._add_unplayed_cost(message.cost)

    def _reap_generation(self, state) -> None:
        task = self._generation_task
        self._generation_task = None
        if task.future.cancelled():
            self._voice_event("cancelled", task, state, reason=task.discard_reason)
            return
        try:
            message, generated = task.future.result()
        except CancelledError:
            self._voice_event("cancelled", task, state, reason=task.discard_reason)
            return
        except Exception:
            self._add_unplayed_cost(None)
            self._voice_event("failed", task, state, reason=task.discard_reason)
            raise
        self._record_generated_result(task, message, generated, state, discarded=True)

    def _finish_listener(self, state, apply=False):
        task = self._listener_task
        self._listener_task = None
        if task.future.cancelled():
            self._voice_event("cancelled", task, state, reason=task.discard_reason)
            return None
        try:
            action = task.future.result()
        except Exception:
            self._add_unplayed_cost(None)
            self._voice_event("failed", task, state, reason=task.discard_reason)
            raise
        # Listener costs are not part of message.cost, even when the resulting
        # decision authorizes a later utterance. Count each decision once.
        if self.enable_user_initiated_interruption:
            self._add_unplayed_cost(action.interrupt_check_cost)
        if self.use_llm_backchannel:
            self._add_unplayed_cost(action.backchannel_check_cost)
        self._voice_event(
            "listener_applied" if apply else "listener_discarded",
            task,
            state,
            reason=task.discard_reason,
            decision=action.model_dump(mode="json"),
        )
        if apply and task.rng is not None:
            state.backchannel_rng.setstate(task.rng.getstate())
        return action if apply else None

    def _refresh_voice_tasks(self, state) -> bool:
        for task in (self._generation_task, self._listener_task):
            if task is not None:
                reason = self._invalid_voice_task(task, state)
                if reason:
                    self._discard_task(task, state, reason)
        reaped = False
        if (
            self._generation_task is not None
            and self._generation_task.discard_reason is not None
            and self._generation_task.future.done()
        ):
            self._reap_generation(state)
            reaped = True
        if (
            self._listener_task is not None
            and self._listener_task.discard_reason is not None
            and self._listener_task.future.done()
        ):
            self._finish_listener(state)
        return reaped

    def get_generation_diagnostics(self) -> dict | None:
        """Report unplayed work separately from actually spoken conversation."""
        if not self.realtime_generation:
            return None
        return {
            "version": VOICE_USER_SIMULATOR_VERSION,
            "additional_llm_cost": self._additional_llm_cost,
            "tts_cost": None,
            "events": list(self._generation_events),
            "candidate_recovery": list(self._candidate_recovery_events),
            "candidate_max_retries": self.candidate_max_retries,
            "candidate_recovery_enabled": self.scenario_constraints is not None,
            "dynamic_followup": list(self._dynamic_followup_events),
            "hidden_intent": list(self._hidden_intent_events),
        }

    def _next_turn_taking_action(
        self, state: UserAudioStreamingState
    ) -> TurnTakingAction:
        if getattr(self, "scenario_transition_pending", False):
            return TurnTakingAction(
                action="wait", info="Continuing to listen during stage transition"
            )
        if not self.realtime_generation:
            return self._compute_turn_taking_action(state)
        if self._refresh_voice_tasks(state):
            return TurnTakingAction(action="wait", info="Discarded obsolete generation")
        if (
            self._saasr_stream is not None
            and not state.is_talking
            and not state.output_streaming_queue
        ):
            queued, done = self._poll_saasr_segment(state)
            if queued:
                # The normal policy below will deliver the newly queued chunk.
                pass
            elif not done:
                return TurnTakingAction(action="wait", info="SAASR audio pending")
            else:
                self._saasr_stream = None
        if self._generation_task is not None:
            task = self._generation_task
            if task.origin == "scripted" and state.agent_speech_active:
                return TurnTakingAction(
                    action="wait", info="Waiting to deliver scripted input"
                )
            if task.discard_reason is None and task.future.done():
                return task.decision.model_copy(deep=True)
            return TurnTakingAction(action="wait", info="Customer generation pending")
        if state.is_talking:
            return self._compute_turn_taking_action(state)
        if self._listener_task is not None:
            if self._listener_task.discard_reason is None:
                if self._listener_task.future.done():
                    return self._finish_listener(state, apply=True)
                return TurnTakingAction(action="wait", info="Listening")
        if state.input_ongoing_speech_duration() > 0:
            if self._listener_task is None and (
                self.interruption_check_interval is None
                or state.tick_count % self.interruption_check_interval == 0
            ):
                snapshot = self._generation_snapshot(state)
                future = self._user_executor.submit(
                    copy_context().run,
                    self._compute_turn_taking_action,
                    snapshot,
                )
                self._listener_task = self._new_voice_task(
                    future,
                    "listener",
                    TurnTakingAction(action="wait"),
                    state,
                    rng=snapshot.backchannel_rng,
                )
            return TurnTakingAction(action="wait", info="Listening")
        if (
            state.input_total_speech_duration() == 0
            and state.tick_count - self._last_generation_tick
            < self.wait_to_respond_threshold_self
        ):
            return TurnTakingAction(
                action="wait", info="Waiting before next generation"
            )
        return self._compute_turn_taking_action(state)

    @staticmethod
    def _generation_snapshot(state: UserAudioStreamingState) -> UserAudioStreamingState:
        # History messages are immutable here; isolate the lists the tick loop updates.
        return state.model_copy(
            update={
                "system_messages": list(state.system_messages),
                "messages": list(state.messages),
                "ticks": list(state.ticks),
                "input_turn_taking_buffer": list(state.input_turn_taking_buffer),
                "output_streaming_queue": list(state.output_streaming_queue),
                "agent_transcript_contents": dict(state.agent_transcript_contents),
                "backchannel_rng": deepcopy(state.backchannel_rng),
            }
        )

    def _start_generation(self, state, tool_result=None, action=None) -> None:
        if self._generation_closed:
            return
        self._latest_state = state
        if action is None:
            action = TurnTakingAction(
                action="generate_message", generation_origin="tool_result"
            )
        snapshot = self._generation_snapshot(state)
        snapshot._llm_generation_seconds = None
        snapshot._tts_synthesis_seconds = None
        self._last_generation_tick = state.tick_count
        if self._listener_task is not None:
            self._discard_task(self._listener_task, state, "generation_started")
        if tool_result is not None:
            snapshot.input_turn_taking_buffer = [tool_result]
        message = (
            snapshot.input_turn_taking_buffer[-1]
            if snapshot.input_turn_taking_buffer
            else None
        )
        if action.action == "backchannel":
            future = self._user_executor.submit(
                copy_context().run, self._generate_backchannel_result, snapshot
            )
        else:
            scripted = self._scenario_inputs[0] if self._scenario_inputs else None
            future = self._user_executor.submit(
                copy_context().run,
                self._generate_full_duplex_voice_message,
                *(
                    (message, snapshot, scripted)
                    if scripted is not None
                    else (message, snapshot)
                ),
            )
        origin = action.generation_origin or "normal"
        if action.action != "backchannel" and scripted is not None:
            origin = "scripted"
        if self._needs_tool_reply and origin == "normal":
            origin = "tool_result"
        action = action.model_copy(update={"generation_origin": origin})
        self._generation_task = self._new_voice_task(future, origin, action, state)

    def _take_generation(self, state, action):
        """Revalidate before any speech, STOP signal, or user tool is committed."""
        task = self._generation_task
        if task is None:
            self._start_generation(state, action=action)
            return None
        reason = self._invalid_voice_task(task, state)
        if reason:
            self._discard_task(task, state, reason)
        if task.discard_reason is not None:
            if task.future.done():
                self._reap_generation(state)
            return None
        if not task.future.done():
            return None
        try:
            message, generated = task.future.result()
        except Exception:
            self._generation_task = None
            self._add_unplayed_cost(None)
            self._voice_event("failed", task, state)
            raise
        self._record_generated_result(task, message, generated, state, discarded=False)
        self._voice_event("committed", task, state)
        self._generation_task = None
        self._sync_hidden_intent_context(state)
        if task.origin == "scripted":
            key = (message.raw_data or {}).get("scenario_input_id")
            if not self._scenario_inputs or self._scenario_inputs[0][0] != key:
                raise RuntimeError("Scripted user input changed before delivery")
            _, source_text = self._scenario_inputs.pop(0)
            released_text = message.content or source_text
            self._released_scenario_facts.append(released_text)
            reminder = UserSimulator.scenario_stage_context(self, key, released_text)
            if reminder is not None:
                state.system_messages.append(reminder)
            state.system_messages.append(
                SystemMessage(
                    role="system",
                    content=(
                        "Additional user information now available: " + released_text
                    ),
                )
            )
        state.user_utterance_count = generated.user_utterance_count
        if task.origin == "backchannel":
            state.backchannel_rng.setstate(generated.backchannel_rng.getstate())
        for name in ("_llm_generation_seconds", "_tts_synthesis_seconds"):
            setattr(state, name, getattr(generated, name, None))
        state.input_turn_taking_buffer = state.input_turn_taking_buffer[
            task.input_count :
        ]
        state.delivering_tool_result_speech = task.from_tool_result
        if task.from_tool_result:
            self._needs_tool_reply = False
        for field, value in task.decision.model_dump().items():
            setattr(action, field, value)
        return message, state

    def _compute_turn_taking_action(
        self, state: UserAudioStreamingState
    ) -> TurnTakingAction:
        """
        Decide the next action to take in the turn-taking.
        """
        if (
            self.silence_checkin_threshold
            and state.user_utterance_count > 0
            and not state.is_talking
            and not self._scenario_inputs
            and not self._needs_tool_reply
            and not any(
                chunk.contains_speech
                or chunk.content
                or isinstance(chunk, EnvironmentMessage)
                for chunk in state.input_turn_taking_buffer
            )
            and min(
                state.time_since_last_talk,
                state.time_since_last_other_talk,
                state.tick_count - self._last_generation_tick,
            )
            < self.silence_checkin_threshold
        ):
            return TurnTakingAction(
                action="wait", info="Waiting before silence check-in"
            )

        # Prepare listener reaction callbacks
        should_interrupt_callback = None
        should_backchannel_callback = None
        integration_ticks = self.integration_ticks

        # Interruption callback is tied to persona's interrupt tendency
        if self.enable_user_initiated_interruption:

            def should_interrupt_callback(
                s: UserStreamingState,
            ) -> ListenerReactionDecision:
                return user_interruption_policy(
                    s,
                    integration_ticks=integration_ticks,
                    decision_model=self.decision_model,
                    decision_model_args=self.decision_model_args,
                )

        # Backchannel callback is tied to the use_llm_backchannel config
        if self.use_llm_backchannel:

            def should_backchannel_callback(
                s: UserStreamingState,
            ) -> ListenerReactionDecision:
                return user_backchannel_policy(
                    s,
                    self.backchannel,
                    integration_ticks=integration_ticks,
                    decision_model=self.decision_model,
                    decision_model_args=self.decision_model_args,
                )

        action, info = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=self.yield_threshold_when_interrupted,
            yield_threshold_when_interrupting=self.yield_threshold_when_interrupting,
            wait_to_respond_threshold_other=self.wait_to_respond_threshold_other,
            wait_to_respond_threshold_self=self.wait_to_respond_threshold_self,
            backchannel_min_threshold=self.backchannel_min_threshold,
            backchannel_max_threshold=self.backchannel_max_threshold,
            backchannel_poisson_rate=self.backchannel_poisson_rate,
            tick_duration_seconds=self.tick_duration_seconds,
            should_interrupt_callback=should_interrupt_callback,
            should_backchannel_callback=should_backchannel_callback,
            use_llm_backchannel=self.use_llm_backchannel,
            listener_reaction_check_interval=self.interruption_check_interval,
        )
        logger.debug(f"USER SIMULATOR TURN-TAKING ACTION: {action}. Reason: {info}")

        # Extract timing/cost metadata from state (set by basic_turn_taking_policy)
        timing = getattr(state, "_listener_reaction_timing", None) or {}

        return TurnTakingAction(
            action=action,
            info=info,
            generation_origin=(
                "backchannel"
                if action == "backchannel"
                else (
                    "interrupt"
                    if state.input_ongoing_speech_duration() > 0
                    else "normal"
                )
                if action == "generate_message"
                else None
            ),
            interrupt_check_seconds=timing.get("interrupt_check_seconds"),
            interrupt_check_cost=timing.get("interrupt_check_cost"),
            interrupt_check_usage=timing.get("interrupt_check_usage"),
            backchannel_check_seconds=timing.get("backchannel_check_seconds"),
            backchannel_check_cost=timing.get("backchannel_check_cost"),
            backchannel_check_usage=timing.get("backchannel_check_usage"),
        )

    def _apply_frame_drop(
        self, user_message: UserMessage, drop_duration_ms: int
    ) -> UserMessage:
        """Apply frame drop (packet loss simulation) by zeroing out audio."""
        zeroed = self.apply_streaming_frame_drop(
            audio_bytes=user_message.get_audio_bytes(),
            sample_rate=user_message.audio_format.sample_rate,
            bytes_per_sample=user_message.audio_format.bytes_per_sample,
            drop_duration_ms=drop_duration_ms,
            is_ulaw=user_message.audio_format.is_ulaw,
        )
        user_message.audio_content = audio_bytes_to_string(zeroed)

        existing = user_message.channel_effects
        total_drop_ms = drop_duration_ms + (existing.frame_drop_ms if existing else 0)
        user_message.channel_effects = ChannelEffectsResult(
            frame_drops_enabled=True,
            frame_drop_ms=total_drop_ms,
        )
        return user_message

    def _add_telephony_compression(
        self,
        user_message: UserMessage,
        converter: StreamingTelephonyConverter,
    ) -> UserMessage:
        """Add telephony compression to the audio."""
        audio_data = AudioData(
            data=user_message.get_audio_bytes(),
            format=user_message.audio_format,
        )
        audio_data = converter.convert_chunk(audio_data)
        user_message.audio_content = audio_bytes_to_string(audio_data.data)
        user_message.audio_format = audio_data.format
        user_message.audio_path = None
        return user_message

    def _apply_chunk_effects(
        self,
        chunk: Optional[UserMessage],
        state: UserAudioStreamingState,
        is_speech: bool,
    ) -> UserMessage:
        """Apply scheduled effects, noise, telephony, and frame drops to chunk.

        Uses the multitrack pipeline: each effect is computed as an
        independent track, then summed before telephony and frame drops.
        When audio taps are enabled, individual tracks are recorded.
        Effect events are always recorded in the timeline.
        """
        if self.saasr_session is not None:
            return self._mix_saasr_chunk(chunk, state)

        if state.noise_generator is None:
            raise RuntimeError("Standard voice rendering requires a noise generator")

        current_time_ms = int(state.elapsed_samples * 1000 / PCM_SAMPLE_RATE)

        # 1. Get scheduled effects from the scheduler
        scheduled_effects = []
        if state.effect_scheduler is not None:
            chunk_duration_ms = int(self.chunk_size * 1000 / PCM_SAMPLE_RATE)
            scheduled_effects = state.effect_scheduler.check_for_effects(
                chunk_duration_ms=chunk_duration_ms,
                is_silence=not is_speech,
                current_time_ms=current_time_ms,
                has_active_burst=state.noise_generator.has_active_burst(),
            )

        if self._audio_taps:
            self._audio_taps["tts_output"].record_message(chunk)

        # 2. Extract speech audio from the message
        speech_audio = None
        if chunk is not None:
            speech_audio = AudioData(
                data=chunk.get_audio_bytes(),
                format=deepcopy(chunk.audio_format),
                audio_path=chunk.audio_path,
            )

        # 3. Process chunk through multitrack pipeline
        result = self.process_streaming_chunk(
            speech_audio=speech_audio,
            noise_generator=state.noise_generator,
            num_samples=self.chunk_size,
            scheduled_effects=scheduled_effects,
            out_of_turn_generator=state.out_of_turn_speech_generator,
            pending_effect=state.pending_effect,
        )
        state.pending_effect = result.pending_effect

        # 4. Record timeline events
        if result.burst_noise_file:
            burst_file_name = Path(result.burst_noise_file).name
            burst_params: dict = {"file": burst_file_name}
            if "burst_snr_db" in result.tracks.metadata:
                burst_params["snr_db"] = result.tracks.metadata["burst_snr_db"]
            if "burst_scale" in result.tracks.metadata:
                burst_params["scale"] = result.tracks.metadata["burst_scale"]
            self._effect_timeline.open_event(
                effect_type="burst_noise",
                start_ms=current_time_ms,
                participant="user",
                params=burst_params,
            )
        if result.triggered_speech_insert:
            self._effect_timeline.open_event(
                effect_type="out_of_turn_speech",
                start_ms=current_time_ms,
                participant="user",
                params={
                    "type": result.triggered_speech_insert.type,
                    "text": result.triggered_speech_insert.text,
                },
            )
        if result.pending_effect_completed:
            self._effect_timeline.close_event(
                effect_type="out_of_turn_speech",
                end_ms=current_time_ms,
                participant="user",
            )
        if result.pending_effect_discarded:
            if self._effect_timeline.has_open_event("out_of_turn_speech", "user"):
                self._effect_timeline.close_event(
                    effect_type="out_of_turn_speech",
                    end_ms=current_time_ms,
                    participant="user",
                )
        # Close burst event when the noise generator's burst finishes
        if (
            not state.noise_generator.has_active_burst()
            and self._effect_timeline.has_open_event("burst_noise", "user")
        ):
            self._effect_timeline.close_event(
                effect_type="burst_noise",
                end_ms=current_time_ms,
                participant="user",
            )

        # 5. Record per-track taps (only when enabled)
        if self._audio_taps:
            self._audio_taps["speech_only"].record_numpy(result.tracks.speech)
            self._audio_taps["background_noise_only"].record_numpy(
                result.tracks.background_noise
            )
            self._audio_taps["burst_noise_only"].record_numpy(result.tracks.burst_noise)
            self._audio_taps["out_of_turn_speech_only"].record_numpy(
                result.out_of_turn_speech
            )

        # 6. Sum tracks -> mixed PCM 16kHz
        mixed_audio = result.to_mixed_audio_data()

        # Build the output message
        if not chunk:
            chunk = UserMessage(
                role="user",
                content="",
                cost=0.0,
                usage=None,
                is_audio=True,
                audio_content=audio_bytes_to_string(mixed_audio.data),
                audio_format=mixed_audio.format,
                audio_script_gold="",
                chunk_id=0,
                is_final_chunk=True,
                contains_speech=False,
                source_effects=result.source_effects,
            )
        else:
            chunk.audio_content = audio_bytes_to_string(mixed_audio.data)
            chunk.audio_format = mixed_audio.format
            chunk.source_effects = result.source_effects

        chunk.raw_data = {**(chunk.raw_data or {})}
        chunk.raw_data.setdefault(
            "user_event_kind", "formal" if is_speech else "background"
        )
        if result.speech_event is not None:
            chunk.raw_data["independent_user_events"] = [result.speech_event]

        if self._audio_taps:
            self._audio_taps["post_noise"].record_message(chunk)

        # 7. Apply telephony compression
        if self.voice_settings.speech_environment.telephony_enabled:
            chunk = self._add_telephony_compression(chunk, state.telephony_converter)

        if self._audio_taps:
            self._audio_taps["post_telephony"].record_message(chunk)

        # 8. Apply frame drops after telephony
        for effect in scheduled_effects:
            if effect.effect_type == "frame_drop" and effect.frame_drop_duration_ms:
                self._effect_timeline.open_event(
                    effect_type="frame_drop",
                    start_ms=current_time_ms,
                    participant="user",
                    params={"duration_ms": effect.frame_drop_duration_ms},
                )
                self._effect_timeline.close_event(
                    effect_type="frame_drop",
                    end_ms=current_time_ms + effect.frame_drop_duration_ms,
                    participant="user",
                )
                chunk = self._apply_frame_drop(chunk, effect.frame_drop_duration_ms)
                logger.debug(f"Applied frame drop: {effect.frame_drop_duration_ms}ms")

        if self._audio_taps:
            self._audio_taps["user_output"].record_message(chunk)

        # Update elapsed samples
        state.elapsed_samples += self.chunk_size

        return chunk

    def _perform_turn_taking_action(
        self, state: UserAudioStreamingState, action: TurnTakingAction
    ) -> Tuple[UserMessage, UserAudioStreamingState]:
        """
        Perform the next action in the turn-taking.

        Note: Chunk recording in tick-based history is handled by get_next_chunk()
        in the StreamingMixin, not here. This method just produces the chunk
        and manages pending buffers.

        Args:
            state: The current state of the turn-taking.
            action: The action to perform.
        Returns:
            A tuple of the next chunk and the updated state.
        """
        logger.debug(f"Performing turn-taking action: {action}")
        next_user_chunk = None
        is_speech_action = False
        if action.action == "keep_talking":
            next_user_chunk = state.output_streaming_queue.pop(0)
            next_user_chunk.timestamp = get_now()
            is_speech_action = True
            # Clear backchannel flag when queue is empty (backchannel complete)
            if len(state.output_streaming_queue) == 0:
                if state.is_backchanneling:
                    state.is_backchanneling = False
                    logger.debug("Backchannel complete")
                if state.delivering_tool_result_speech:
                    state.delivering_tool_result_speech = False
                    logger.debug("Tool result speech delivery complete")
        elif action.action == "stop_talking":
            logger.debug("Stopping talking: Flushing output streaming queue")
            state.output_streaming_queue = []
            state.is_backchanneling = False
            if self._saasr_stream is not None:
                self._saasr_stream.cancel()
                self._saasr_stream = None
        elif action.action == "generate_message":
            if self.realtime_generation:
                generated = self._take_generation(state, action)
                if generated is None:
                    return self._emit_waiting_chunk(state)
                full_message, new_state = generated
            else:
                merged_message = merge_homogeneous_chunks(
                    state.input_turn_taking_buffer
                )
                full_message, new_state = self._generate_full_duplex_voice_message(
                    merged_message, state
                )
                state.input_turn_taking_buffer = []
            if full_message.is_tool_call():
                logger.debug("Generating message: Tool call detected")
                noise_chunk = self._apply_chunk_effects(
                    None, new_state, is_speech=False
                )
                full_message.audio_content = noise_chunk.audio_content
                full_message.audio_format = noise_chunk.audio_format
                full_message.is_audio = True
                full_message.contains_speech = False
                full_message.source_effects = noise_chunk.source_effects
                full_message.turn_taking_action = action
                return full_message, new_state
            elif self.is_stop(full_message):
                full_message.raw_data = {
                    **(full_message.raw_data or {}),
                    "user_event_kind": "control",
                }
                logger.debug("Generating message: Stop message detected")
                full_message.turn_taking_action = action
                return full_message, new_state
            elif not full_message.has_text_content():
                logger.info(
                    "Customer returned no speech or tools; continuing to listen"
                )
                return self._emit_waiting_chunk(state)
            else:
                logger.debug("Generating message: Creating chunk messages")
                self._record_spelling_events(full_message, state)
                chunk_messages = self._create_chunk_messages(full_message)
                logger.debug(
                    f"Generating message: Created {len(chunk_messages)} chunk messages"
                )
                state.output_streaming_queue.extend(chunk_messages)
                next_user_chunk = state.output_streaming_queue.pop(0)
                next_user_chunk.timestamp = get_now()
                is_speech_action = True
        elif action.action == "wait":
            logger.debug("Waiting: No action required")
        elif action.action == "backchannel":
            logger.debug("Backchannel: Generating backchannel message")
            if self.realtime_generation:
                generated = self._take_generation(state, action)
                if generated is None:
                    return self._emit_waiting_chunk(state)
                backchannel_message, state = generated
            else:
                backchannel_message = self._generate_backchannel_message(state)
            chunk_messages = self._create_chunk_messages(backchannel_message)
            logger.debug(f"Backchannel: Created {len(chunk_messages)} chunk messages")
            state.output_streaming_queue.extend(chunk_messages)
            next_user_chunk = state.output_streaming_queue.pop(0)
            next_user_chunk.timestamp = get_now()
            is_speech_action = True
            # Mark that we're delivering a backchannel (prevents interruption)
            state.is_backchanneling = True
            # Reset backchannel cooldown timer after performing backchannel
            state.ticks_since_last_backchannel = 0
        else:
            raise ValueError(f"Invalid action: {action}")

        next_user_chunk = self._apply_chunk_effects(
            next_user_chunk, state, is_speech=is_speech_action
        )

        # _apply_chunk_effects should always return a chunk (creates one with noise if None)
        if next_user_chunk is None:
            raise ValueError(
                "Voice Streaming User Simulator should never return None chunk."
            )

        if self.saasr_session is not None:
            is_speech_action = bool(next_user_chunk.contains_speech)
        if is_speech_action:
            next_user_chunk.contains_speech = True
            state.time_since_last_talk = 0
        else:
            state.time_since_last_talk += 1

        # Update turn-taking action with LLM/TTS timing if available
        if action.action in {"generate_message", "backchannel"}:
            llm_time = getattr(state, "_llm_generation_seconds", None)
            tts_time = getattr(state, "_tts_synthesis_seconds", None)
            if llm_time is not None:
                action.llm_generation_seconds = llm_time
            if tts_time is not None:
                action.tts_synthesis_seconds = tts_time
            # Clear the temporary attributes
            state._llm_generation_seconds = None
            state._tts_synthesis_seconds = None

        # Set the turn-taking action on the chunk
        next_user_chunk.turn_taking_action = action
        return next_user_chunk, state

    def _process_tool_result(
        self,
        tool_result: EnvironmentMessage,
        state: UserAudioStreamingState,
    ) -> Tuple[UserMessage, UserAudioStreamingState]:
        """Process a tool result by calling the LLM and returning the response."""
        if self.realtime_generation:
            if self._generation_task is not None:
                raise RuntimeError(
                    "Tool result arrived while customer generation is pending"
                )
            self._needs_tool_reply = True
            if not state.agent_speech_active:
                self._start_generation(state, tool_result)
            return self._emit_waiting_chunk(state)
        # Temporarily set buffer so get_linearized_messages(include_pending_input=True)
        # picks up the tool result for LLM context
        saved_buffer = state.input_turn_taking_buffer
        state.input_turn_taking_buffer = [tool_result]

        full_message, state = self._generate_full_duplex_voice_message(
            tool_result, state
        )

        # Restore participant chunks (preserved for timing)
        state.input_turn_taking_buffer = saved_buffer

        if full_message.is_tool_call():
            logger.debug("Tool result processing: LLM returned another tool call")
            noise_chunk = self._apply_chunk_effects(None, state, is_speech=False)
            full_message.audio_content = noise_chunk.audio_content
            full_message.audio_format = noise_chunk.audio_format
            full_message.is_audio = True
            full_message.contains_speech = False
            full_message.source_effects = noise_chunk.source_effects
            return full_message, state
        elif self.is_stop(full_message):
            logger.debug("Tool result processing: stop message detected")
            return full_message, state
        else:
            # Queue chunks but DON'T start streaming yet.
            # Speech delivery is deferred to normal turn-taking on the next tick.
            # This ensures overlap/interruption logic applies correctly.
            logger.debug("Tool result processing: queuing speech chunks")
            chunk_messages = self._create_chunk_messages(full_message)
            state.output_streaming_queue.extend(chunk_messages)
            state.delivering_tool_result_speech = True
            waiting_chunk, state = self._emit_waiting_chunk(state)
            return waiting_chunk, state

    def _emit_waiting_chunk(
        self, state: UserAudioStreamingState
    ) -> Tuple[UserMessage, UserAudioStreamingState]:
        """Emit background noise while waiting for tool results."""
        noise_chunk = self._apply_chunk_effects(None, state, is_speech=False)
        state.time_since_last_talk += 1
        return noise_chunk, state

    def queue_scenario_input(self, event_id: str, text: str) -> None:
        """Queue one released input through the same TTS and playback pipeline."""
        if self._saasr_stream is not None:
            self._saasr_stream.cancel()
            self._saasr_stream = None
        if not any(key == event_id for key, _ in self._scenario_inputs):
            self._scenario_inputs.append((event_id, text))
            if (
                self._generation_task is not None
                and self._generation_task.origin != "scripted"
            ):
                self._discard_task(
                    self._generation_task,
                    self._latest_state,
                    "scripted_input_available",
                )

    def observe_agent_tool_calls(self, tool_calls) -> None:
        """Bind Agent tool calls to hidden-intent state for diagnostics."""
        if self.hidden_intent_runtime is None:
            return
        for call in tool_calls:
            self.hidden_intent_runtime.observe_tool_call(call.name, call.arguments)

    def get_hidden_intent_metrics(self) -> dict | None:
        """Return the single public hidden-intent metric for this run."""
        if self.hidden_intent_runtime is None:
            return None
        from tau2.metrics.hidden_intent import score_hidden_intent

        return score_hidden_intent(
            self.hidden_intent_runtime.graph, self.hidden_intent_runtime.state
        )

    def _generate_full_duplex_voice_message(
        self,
        message: ValidUserInputMessage,
        state: UserAudioStreamingState,
        scripted_input=None,
    ) -> Tuple[UserMessage, UserAudioStreamingState]:
        """
        Generate a voice message using tick-based history for LLM context.

        This method linearizes the tick history to build the LLM context,
        and synthesizes voice for the response.

        Args:
            message: The incoming message to respond to.
            state: The current streaming state with tick history.

        Returns:
            A tuple of the user message (with voice) and updated state.
        """
        # Build LLM context from linearized ticks (including current pending input)
        linearized_messages = state.get_linearized_messages(
            strategy=LinearizationStrategy.CONTAINMENT_AWARE,
            include_pending_input=True,
            indicate_current_incomplete=True,
            integration_ticks=self.integration_ticks,
            silence_annotation_threshold_ticks=self.silence_annotation_threshold_ticks,
            tick_duration_seconds=self.tick_duration_seconds,
        )

        # Check that last message is a valid user input message
        if linearized_messages and not isinstance(
            linearized_messages[-1], (ValidUserInputMessage)
        ):
            if isinstance(linearized_messages[-1], SystemMessage):
                # SystemMessage at end is expected (e.g., silence annotations)
                logger.debug(
                    f"Running user generation with SystemMessage as last message (e.g., silence annotation)"
                )
            else:
                logger.warning(
                    f"Last message is not a valid user input message: {type(linearized_messages[-1]).__name__}"
                )

        if linearized_messages and isinstance(
            linearized_messages[-1], (AssistantMessage)
        ):
            if linearized_messages[-1].content is None:
                logger.warning(
                    f"Last message is an assistant message with no content: {linearized_messages[-1]}"
                )

        if self.hidden_intent_runtime is not None:
            for message in reversed(linearized_messages):
                if not isinstance(message, AssistantMessage) or not message.content:
                    continue
                self._observe_hidden_question(message.content, state)
                break
            self._sync_hidden_intent_context(state)

        # Flip roles for user simulator (it sees itself as assistant)
        flipped_messages = self._flip_roles_for_llm(linearized_messages)

        # Build full message list with system messages
        messages = state.system_messages + flipped_messages

        # Add a role identity reminder as a system message to help the LLM stay in character
        role_reminder = SystemMessage(
            role="system",
            content=self.role_reminder_override
            or "REMINDER: You are the CUSTOMER calling for help. Respond as the customer would - with questions, requests, or information about your issue. Do NOT respond as the customer service agent.",
        )
        messages = messages + [role_reminder]

        # Generate response (timing is captured in llm_utils.generate)
        if (
            scripted_input is None
            and not self.realtime_generation
            and self._scenario_inputs
        ):
            scripted_input = self._scenario_inputs.pop(0)
        if scripted_input is not None:
            key, text = scripted_input
            dynamic_event = None
            dynamic_stage = self.scenario_dynamic_stage_ids.get(key)
            dynamic_enabled = (
                self.dynamic_followup_config is not None
                and self.dynamic_followup_config.enabled
                and dynamic_stage in self.dynamic_followup_config.stages
            )
            if dynamic_enabled:
                followup_model, followup_args = self._api_settings("FOLLOWUP")
                cache_material = {
                    "model": followup_model,
                    "model_args": {
                        k: v for k, v in followup_args.items() if k != "api_key"
                    },
                    "event_id": key,
                    "stage": dynamic_stage,
                    "source_request": text,
                    "state_revision": getattr(state, "agent_input_revision", 0),
                    "config": self.dynamic_followup_config.model_dump(mode="json"),
                }
                cache_key = hashlib.sha256(
                    json.dumps(
                        cache_material, ensure_ascii=False, sort_keys=True
                    ).encode()
                ).hexdigest()
                cached = self._dynamic_followup_cache.get(cache_key)
                if cached is not None:
                    text = cached["generated_utterance"]
                    dynamic_event = {**cached, "replayed": True}
                else:
                    context_classification = None
                    context_reply = None
                    context_fallback = False
                    context_error = None
                    try:
                        context_classification, context_reply = classify_status_affect(
                            model=followup_model,
                            model_args=followup_args,
                            conversation=linearized_messages,
                            max_context_characters=(
                                self.dynamic_followup_config.max_context_characters
                            ),
                        )
                        stage_status = context_classification.status
                        affect = context_classification.affect
                    except Exception as error:
                        # Context classification is advisory. Preserve the
                        # source request if this extra call is unavailable.
                        stage_status, affect = infer_status_affect(
                            linearized_messages, self._released_scenario_facts
                        )
                        context_fallback = True
                        context_error = f"{type(error).__name__}: {error}"
                    dynamic_context_hash = hashlib.sha256(
                        json.dumps(
                            {
                                "stage": dynamic_stage,
                                "source_request": text,
                                "conversation": [
                                    {"role": m.role, "content": m.content}
                                    for m in linearized_messages
                                    if m.content
                                ],
                                "released_facts": self._released_scenario_facts,
                                "stage_status": stage_status,
                                "affect": affect,
                                "context_fallback": context_fallback,
                                "tool_call_gt": self.scenario_dynamic_stage_tool_gts.get(
                                    key, []
                                ),
                                "config": self.dynamic_followup_config.model_dump(
                                    mode="json"
                                ),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
                    try:
                        (
                            candidate,
                            candidate_reply,
                            verification,
                            verification_reply,
                        ) = generate_dynamic_followup(
                            model=followup_model,
                            model_args=followup_args,
                            stage=dynamic_stage,
                            source_request=text,
                            conversation=linearized_messages,
                            released_facts=self._released_scenario_facts,
                            config=self.dynamic_followup_config,
                            tool_call_gt=self.scenario_dynamic_stage_tool_gts.get(
                                key, []
                            ),
                            stage_status=stage_status,
                            affect=affect,
                        )
                        text = candidate.utterance
                        dynamic_event = {
                            "stage": dynamic_stage,
                            "source_request": scripted_input[1],
                            "generated_utterance": text,
                            "tool_call_gt": self.scenario_dynamic_stage_tool_gts.get(
                                key, []
                            ),
                            "covered_requirements": candidate.covered_requirements,
                            "referenced_entities": candidate.referenced_entities,
                            "new_requirements": candidate.new_requirements,
                            "verification": verification.model_dump(mode="json"),
                            "accepted": True,
                            "fallback": False,
                            "model": followup_model,
                            "prompt_hash": dynamic_context_hash,
                            "context_revision": state.agent_input_revision,
                            "generation_time_seconds": candidate_reply.generation_time_seconds,
                            "verification_cost": verification_reply.cost,
                            "verification_time_seconds": (
                                verification_reply.generation_time_seconds
                            ),
                            "stage_status": stage_status,
                            "affect": affect,
                            "context_classification": (
                                context_classification.model_dump(mode="json")
                                if context_classification is not None
                                else None
                            ),
                            "context_fallback": context_fallback,
                            "context_error": context_error,
                            "context_generation_time_seconds": (
                                context_reply.generation_time_seconds
                                if context_reply is not None
                                else None
                            ),
                            "context_cost": (
                                context_reply.cost
                                if context_reply is not None
                                else None
                            ),
                        }
                    except Exception as error:
                        dynamic_event = {
                            "stage": dynamic_stage,
                            "source_request": text,
                            "generated_utterance": text,
                            "tool_call_gt": self.scenario_dynamic_stage_tool_gts.get(
                                key, []
                            ),
                            "accepted": False,
                            "fallback": True,
                            "reason": type(error).__name__,
                            "reason_detail": str(error),
                            "prompt_hash": dynamic_context_hash,
                            "context_revision": state.agent_input_revision,
                            "stage_status": stage_status,
                            "affect": affect,
                            "context_classification": (
                                context_classification.model_dump(mode="json")
                                if context_classification is not None
                                else None
                            ),
                            "context_fallback": context_fallback,
                            "context_error": context_error,
                        }
                    self._dynamic_followup_cache[cache_key] = dynamic_event
            if dynamic_event is not None:
                self._dynamic_followup_events.append(dynamic_event)
            assistant_message = AssistantMessage(
                role="assistant",
                content=text,
                cost=0.0,
                generation_time_seconds=0.0,
                raw_data={
                    "scenario_input_id": key,
                    **(
                        {"dynamic_followup": dynamic_event}
                        if dynamic_event is not None
                        else {}
                    ),
                },
            )
            if (
                dynamic_event is not None
                and dynamic_event["accepted"]
                and not dynamic_event.get("replayed")
            ):
                assistant_message.cost = candidate_reply.cost
                assistant_message.generation_time_seconds = (
                    candidate_reply.generation_time_seconds
                )
        else:

            def generate_reply(feedback):
                return generate(
                    model=self.llm,
                    messages=messages + feedback,
                    tools=self.tools,
                    call_name="user_streaming_response",
                    **self.llm_args,
                )

            if self.scenario_constraints is None:
                assistant_message = generate_reply([])
            else:
                from tau2.user.grounding import generate_grounded_reply

                def recheck(evidence):
                    if self.audio_recheck is None:
                        return {"status": "unavailable"}
                    latest_text = []
                    found_agent = False
                    # Only the latest actually-heard Agent turn is eligible.
                    for entry in reversed(linearized_messages):
                        if entry.role == "user" and found_agent:
                            break
                        if entry.role == "assistant" and entry.content:
                            found_agent = True
                            latest_text.append(entry.content)
                    heard = " ".join(" ".join(t.split()) for t in reversed(latest_text))
                    if " ".join(evidence.split()) not in heard:
                        return {"status": "unavailable"}
                    # Message merging intentionally drops raw_data. Select IDs
                    # from the original immutable snapshot, then let the observer
                    # bind them to its measured, already-played audio ranges.
                    chunks = [t.other_chunk for t in state.ticks]
                    chunks.extend(state.input_turn_taking_buffer)
                    chunks.extend(linearized_messages)
                    fragments = {}
                    for chunk in chunks:
                        if not isinstance(chunk, AssistantMessage):
                            continue
                        raw = chunk.raw_data or {}
                        metadata = (
                            raw.get("live_segment") or raw.get("live_transcript") or {}
                        )
                        for fragment in metadata.get("fragments", []):
                            if fragment.get("event_id"):
                                fragments[fragment["event_id"]] = fragment
                    return self.audio_recheck(evidence, list(fragments.values()))

                grounding_model, grounding_args = self._api_settings(
                    "USER_GROUNDING",
                    self.decision_model or self.llm,
                    self.decision_model_args if self.decision_model else self.llm_args,
                )
                assistant_message = generate_grounded_reply(
                    generate_reply=generate_reply,
                    allowed=self.scenario_constraints
                    + "\n"
                    + "\n".join(self._released_scenario_facts),
                    conversation="\n".join(
                        f"{m.role}: {m.content}"
                        for m in linearized_messages
                        if m.content
                    ),
                    model=grounding_model,
                    model_args=grounding_args,
                    is_stop=self.is_stop,
                    events=self._candidate_recovery_events,
                    max_retries=self.candidate_max_retries,
                    recheck=recheck if self.audio_recheck is not None else None,
                    context_revision=state.agent_input_revision,
                )

        # Store LLM timing on state for later use when updating TurnTakingAction
        # Use generation_time_seconds from the returned message
        state._llm_generation_seconds = assistant_message.generation_time_seconds

        # Convert assistant response to user message
        user_message = UserMessage(
            role="user",
            content=assistant_message.content,
            cost=assistant_message.cost,
            usage=assistant_message.usage,
            raw_data=assistant_message.raw_data,
        )

        my_str = ""
        for message in linearized_messages:
            my_str += f"{message.role}: {message.content}\n"

        logger.info(
            f"USER SIMULATOR:\nSent to LLM:\n{my_str}\nReceived from LLM:\n{user_message.content}\n\n\n"
        )

        # Flip the requestor of tool calls
        if assistant_message.tool_calls is not None:
            user_message.tool_calls = []
            for tool_call in assistant_message.tool_calls:
                user_message.tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        requestor="user",
                    )
                )
            return user_message, state

        # Check for stop
        if self.is_stop(user_message):
            return user_message, state

        if not user_message.has_text_content():
            return user_message, state

        if self.saasr_session is not None:
            return self._generate_saasr_first_segment(user_message, state)

        # Synthesize voice (without background noise - added per chunk) with timing
        effects_turn_idx = state.user_utterance_count
        tts_start = time.perf_counter()
        user_message = self.synthesize_voice(
            user_message,
            state,
            effects_turn_idx=effects_turn_idx,
            add_background_noise=False,
            add_burst_noise=False,
            add_telephony_format=False,
            add_channel_effects=False,
        )
        tts_synthesis_seconds = time.perf_counter() - tts_start

        # Store TTS timing on state for later use
        state._tts_synthesis_seconds = tts_synthesis_seconds

        state.user_utterance_count += 1

        if scripted_input is not None and not self.realtime_generation:
            self._released_scenario_facts.append(scripted_input[1])
            reminder = UserSimulator.scenario_stage_context(self, *scripted_input)
            if reminder is not None:
                state.system_messages.append(reminder)

        return user_message, state

    @staticmethod
    def _split_saasr_segments(text: str) -> list[str]:
        """Split a caller reply at sentence boundaries for first-audio release."""
        segments = [
            segment.strip() for segment in re.split(r"(?<=[.!?。！？])\s+", text)
        ]
        return [segment for segment in segments if segment]

    def _generate_saasr_first_segment(self, message, state):
        """Generate the first SAASR sentence and queue later sentences."""
        from tau2.voice.saasr import SAASR_AUDIO_FORMAT

        segments = self._split_saasr_segments(message.content or "")
        if not segments:
            return message, state
        base_raw_data = {
            **(message.raw_data or {}),
            "user_event_kind": "formal",
            "saasr_utterance_id": str(uuid.uuid4()),
        }
        stream = self.saasr_session.synthesize_stream(
            segments, base_raw_data=base_raw_data
        )
        started = time.perf_counter()
        first = stream.wait_first()
        state._tts_synthesis_seconds = time.perf_counter() - started
        if first is None:
            stream.close()
            return message, state
        self._saasr_stream = None if first["final"] else stream
        first_message = UserMessage(
            role="user",
            content=first["text"],
            cost=message.cost,
            usage=message.usage,
            is_audio=True,
            audio_content=audio_bytes_to_string(first["audio"]),
            audio_format=SAASR_AUDIO_FORMAT.model_copy(),
            audio_script_gold=first["text"],
            raw_data={
                **base_raw_data,
                "saasr_foreground": first["metadata"],
                "saasr_segment_final": first["final"],
            },
        )
        state.user_utterance_count += 1
        return first_message, state

    def _poll_saasr_segment(self, state) -> tuple[bool, bool]:
        """Move one completed SAASR sentence into the normal output queue."""
        stream = self._saasr_stream
        if stream is None:
            return False, True
        item, done = stream.poll()
        if item is None:
            return False, done
        from tau2.voice.saasr import SAASR_AUDIO_FORMAT

        message = UserMessage(
            role="user",
            content=item["text"],
            cost=0.0,
            usage=None,
            is_audio=True,
            audio_content=audio_bytes_to_string(item["audio"]),
            audio_format=SAASR_AUDIO_FORMAT.model_copy(),
            audio_script_gold=item["text"],
            raw_data={
                **stream.base_raw_data,
                "saasr_foreground": item["metadata"],
                "saasr_segment_final": item["final"],
            },
        )
        state.output_streaming_queue.extend(self._create_chunk_messages(message))
        return True, False

    def synthesize_voice(self, message, state, **kwargs):
        """Route all caller text, including backchannels, through one renderer."""
        if self.saasr_session is None:
            return super().synthesize_voice(message, state, **kwargs)
        from tau2.voice.saasr import SAASR_AUDIO_FORMAT
        from tau2.voice.utils.pronunciation_swap import apply_pronunciations

        text = message.content
        if self.voice_settings.pronunciation_map:
            text, _ = apply_pronunciations(
                text,
                self.voice_settings.pronunciation_map,
                grapheme_tokens=tuple(
                    self.voice_settings.pronunciation_grapheme_tokens
                ),
            )
        tables = self.voice_settings.synthesis_config.spelled_entity_tables
        if tables is not None:
            from tau2.voice.spelled_entity_normalizer import normalize_spelled_entities

            text = normalize_spelled_entities(text, tables)
        audio, metadata = self.saasr_session.synthesize(text)
        message.audio_content = audio_bytes_to_string(audio)
        message.audio_format = SAASR_AUDIO_FORMAT.model_copy()
        message.audio_script_gold = text
        message.is_audio = True
        message.raw_data = {**(message.raw_data or {}), "saasr_foreground": metadata}
        return message

    def _create_chunk_messages(self, full_message):
        """Align caller captions to foreground, excluding prelude and RIR tail."""
        full_message.raw_data = {
            "user_event_kind": "formal",
            **(full_message.raw_data or {}),
        }
        if self.saasr_session is None:
            return super()._create_chunk_messages(full_message)
        lead = self.saasr_session.commit(full_message)
        metadata = full_message.raw_data["saasr_foreground"]
        audio = b"\x00\x00" * lead + full_message.get_audio_bytes()
        full_message.audio_content = audio_bytes_to_string(audio)
        chunks = self._chunk_by_bytes(full_message)
        first = lead // self.chunk_size
        last = (lead + metadata["speech_samples"] - 1) // self.chunk_size
        spoken = self._create_content_chunks(full_message.content, last - first + 1)
        script = self._create_content_chunks(
            full_message.audio_script_gold, last - first + 1
        )
        text_chunks = [""] * len(chunks)
        text_chunks[first : last + 1] = spoken
        script_chunks = [""] * len(chunks)
        script_chunks[first : last + 1] = script
        utterance = (full_message.raw_data or {}).get("saasr_utterance_id") or str(
            uuid.uuid4()
        )
        template = "".join(
            f"<chunk id={i}>{text}</chunk>" for i, text in enumerate(script_chunks)
        )
        result = []
        for index, audio_chunk in enumerate(chunks):
            raw = {
                "saasr_foreground": {
                    "transport_gain": metadata["transport_gain"],
                    "utterance": metadata["utterance"],
                },
                "speech_range_seconds": [
                    max(0, lead - index * self.chunk_size) / PCM_SAMPLE_RATE,
                    min(
                        self.chunk_size,
                        lead + metadata["speech_samples"] - index * self.chunk_size,
                    )
                    / PCM_SAMPLE_RATE,
                ]
                if first <= index <= last
                else [0.0, 0.0],
                "user_event_kind": full_message.raw_data["user_event_kind"],
                "saasr_main_final": (
                    index == last
                    and (full_message.raw_data or {}).get("saasr_segment_final", True)
                ),
                "saasr_speech_offset_seconds": max(0, lead - index * self.chunk_size)
                / PCM_SAMPLE_RATE,
            }
            if index == first:
                raw.update(
                    {
                        k: v
                        for k, v in full_message.raw_data.items()
                        if k != "saasr_foreground"
                    }
                )
            # Keep the dry speech boundary separate from reverberant tail audio.
            active = first <= index <= last
            result.append(
                UserMessage(
                    role="user",
                    content=text_chunks[index],
                    is_audio=True,
                    audio_content=audio_bytes_to_string(audio_chunk.data),
                    audio_format=audio_chunk.format,
                    contains_speech=active,
                    utterance_ids=[utterance],
                    chunk_id=index,
                    is_final_chunk=index == len(chunks) - 1,
                    audio_script_gold=f'<message uuid="{utterance}" active="{index}">{template}</message>',
                    raw_data=raw,
                    cost=full_message.cost if index == first else 0.0,
                    usage=full_message.usage if index == first else None,
                )
            )
        return result

    def _mix_saasr_chunk(self, chunk, state):
        """Add SAASR scene audio and an optional interaction insert."""
        from tau2.voice.saasr import SAASR_AUDIO_FORMAT

        insert_result = None
        if self.interaction_insert_engine is not None:
            insert_result = self.interaction_insert_engine.process(
                is_speech=bool(chunk and chunk.contains_speech),
                num_samples=self.chunk_size,
                pending=state.interaction_insert,
            )
            state.interaction_insert = insert_result.pending

        audio, output_id = self.saasr_session.mix_tick(
            chunk,
            self.chunk_size,
            interaction_insert=(insert_result.audio if insert_result else None),
        )
        if chunk is None:
            chunk = UserMessage(
                role="user",
                content="",
                contains_speech=False,
                audio_script_gold="",
                is_audio=True,
            )
        chunk.audio_content = audio_bytes_to_string(audio)
        chunk.audio_format = SAASR_AUDIO_FORMAT.model_copy()
        chunk.audio_path = None
        chunk.raw_data = {**(chunk.raw_data or {}), "saasr_output_id": output_id}
        if insert_result is not None:
            if insert_result.triggered is not None:
                chunk.raw_data["interaction_insert"] = {
                    "type": insert_result.triggered.type,
                    "text": insert_result.triggered.text,
                }
            if insert_result.event is not None:
                chunk.raw_data["interaction_insert_event"] = insert_result.event
                self.saasr_session.events.append(
                    {
                        "event": "interaction_insert",
                        **insert_result.event,
                        "completed": insert_result.completed,
                    }
                )
            if insert_result.discarded:
                self.saasr_session.events.append(
                    {"event": "interaction_insert_discarded"}
                )
        chunk.raw_data.setdefault(
            "user_event_kind", "background" if not chunk.contains_speech else "formal"
        )
        if self._audio_taps:
            self._audio_taps["user_output"].record_message(chunk)
        state.elapsed_samples += self.chunk_size
        return chunk

    def _record_spelling_events(self, message, state) -> None:
        """Attach spelling effects only after an utterance is accepted to play."""
        if self.spell_event_detector is not None:
            now_ms = int(state.elapsed_samples * 1000 / PCM_SAMPLE_RATE)
            for detection in self.spell_event_detector(message.content):
                event = self._effect_timeline.open_event(
                    "spell_out",
                    start_ms=now_ms,
                    participant="user",
                    params=detection.model_dump(),
                )
                event.end_ms = now_ms

    def _flip_roles_for_llm(self, messages: list[Message]) -> list[Message]:
        """
        Flip message roles for user simulator LLM context.

        The user simulator acts as an assistant internally, so:
        - UserMessage (what user said) -> AssistantMessage (what "I" said)
        - AssistantMessage (what agent said) -> UserMessage (what "they" said)
        - ToolMessage for user -> kept as-is (user's tool results)
        - SystemMessage -> kept as-is (e.g., silence annotations)

        Args:
            messages: The linearized message history.

        Returns:
            Messages with roles flipped for LLM consumption.
        """
        from tau2.data_model.message import SystemMessage, ToolMessage

        flipped = []
        for msg in messages:
            if isinstance(msg, UserMessage):
                # User's message -> becomes assistant response.
                # Skip silent/whitespace-only turns (empty STT, or a proportional
                # transcript slice landing on an inter-word space): they carry no
                # info and would fail validate_message_history, which strips
                # whitespace. Mirrors the agent-turn guard below.
                if (msg.content and msg.content.strip()) or msg.is_tool_call():
                    flipped.append(
                        AssistantMessage(
                            role="assistant",
                            tool_calls=msg.tool_calls,
                            content=msg.content,
                        )
                    )
            elif isinstance(msg, AssistantMessage):
                # Agent's message -> becomes user input
                # Skip tool calls and messages without text content
                # (audio-only messages can't be converted to text UserMessage)
                if not msg.is_tool_call() and msg.content and msg.content.strip():
                    flipped.append(
                        UserMessage(
                            role="user",
                            content=msg.content,
                        )
                    )
            elif isinstance(msg, ToolMessage):
                # Tool messages for user are kept
                if msg.requestor == "user":
                    flipped.append(
                        ToolMessage(
                            id=msg.id,
                            role=msg.role,
                            content=msg.content,
                        )
                    )
            elif isinstance(msg, SystemMessage):
                # System messages are kept as-is (e.g., silence annotations)
                flipped.append(msg)
        return flipped

    def _generate_backchannel_message(
        self, state: UserAudioStreamingState
    ) -> UserMessage:
        """
        Generate a backchannel message.
        """
        # Use user_utterance_count for streaming mode (state.messages isn't updated)
        effects_turn_idx = state.user_utterance_count

        # Randomly select a backchannel phrase
        content = state.backchannel_rng.choice(self.backchannel.phrases)

        user_message = UserMessage(
            role="user",
            content=content,
            cost=0.0,
            usage=None,
            raw_data={"user_event_kind": "backchannel"},
            chunk_id=0,
            is_final_chunk=True,
        )
        state.user_utterance_count += 1
        return self.synthesize_voice(
            message=user_message,
            state=state,
            effects_turn_idx=effects_turn_idx,
            add_background_noise=False,
            add_burst_noise=False,
            add_telephony_format=False,
            add_channel_effects=False,
        )

    def _generate_backchannel_result(self, state):
        started = time.perf_counter()
        message = self._generate_backchannel_message(state)
        state._tts_synthesis_seconds = time.perf_counter() - started
        return message, state


def _format_conversation_history(messages: list[Message]) -> str:
    """Format conversation history for interruption decision prompt."""
    formatted_lines = []
    for msg in messages[-100:]:  # Last 100 messages for context
        role = msg.role.upper()
        content = msg.content or ""
        # Skip empty messages
        if content:
            formatted_lines.append(f"{role}: {content}")
    return "\n".join(formatted_lines)

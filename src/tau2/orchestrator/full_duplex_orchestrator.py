"""
Full-duplex orchestrator for streaming/voice communication.

This extends the BaseOrchestrator to support simultaneous bidirectional
communication between agent and user.
"""

import time
from bisect import bisect_left
from copy import deepcopy
from typing import Any, Optional, TypeVar, Union

from loguru import logger

from tau2.agent.base.streaming import compute_responsiveness_info
from tau2.agent.base_agent import FullDuplexAgent
from tau2.data_model.language import CallContext
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    Tick,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.data_model.simulation import SimulationRun, TerminationReason
from tau2.data_model.tasks import Task
from tau2.environment.environment import Environment
from tau2.orchestrator.modes import CommunicationMode
from tau2.orchestrator.orchestrator import DEFAULT_FIRST_AGENT_MESSAGE, BaseOrchestrator
from tau2.orchestrator.watchdog import (
    WatchdogController,
    WatchdogVerdict,
    compact_watchdog_context,
)
from tau2.user.user_simulator import UserSimulator
from tau2.user.user_simulator_base import FullDuplexUser, UserError
from tau2.utils.llm_utils import get_cost
from tau2.utils.utils import get_now
from tau2.voice.audio_activity import is_speaking
from tau2.voice.pricing import build_session_usage
from tau2.voice.saasr_worker import SaasrWorkerError
from tau2.voice.utils.transcript_utils import compute_proportional_user_transcripts

# Type variables for generic full-duplex orchestrator
StreamingAgentT = TypeVar("StreamingAgentT", bound=FullDuplexAgent)
StreamingUserT = TypeVar("StreamingUserT", bound=FullDuplexUser)


def _merge_final_live_transcript(ticks, final_live_transcript):
    """Attach final observer text without mixing session and tick clocks."""

    if final_live_transcript is None or not final_live_transcript.fragments:
        return
    for tick in reversed(ticks):
        if tick.agent_chunk is None:
            continue
        raw = dict(tick.agent_chunk.raw_data or {})
        existing = raw.get("live_transcript")
        if existing is None:
            final_data = final_live_transcript.model_dump(mode="json")
            # The final observer intervals are session-relative. This field is
            # interpreted as tick-relative evidence, so do not place them here.
            final_data["speech_intervals_ms"] = []
            raw["live_transcript"] = final_data
        else:
            from tau2.data_model.transcript import LiveTranscriptTick

            merged = LiveTranscriptTick.model_validate(existing)
            merged.fragments.extend(final_live_transcript.fragments)
            # Existing tick audio already supplies the local activity spans.
            # Finalized intervals use the session clock and must stay separate.
            raw["live_transcript"] = merged.model_dump(mode="json")
        tick.agent_chunk.raw_data = raw
        break


class FullDuplexOrchestrator(BaseOrchestrator[StreamingAgentT, StreamingUserT, Tick]):
    """
    Orchestrator for full-duplex (streaming) communication.

    Extends the BaseOrchestrator to support:
    - Simultaneous agent and user communication
    - Chunk-based message passing
    - Tick-based trajectory that groups concurrent events

    Key differences from half-duplex (Orchestrator):
    - Uses get_next_chunk() instead of generate_next_message()
    - Both agent and user can "speak" at the same time
    - Messages are chunked rather than complete
    - Trajectory is organized by ticks, not individual messages

    Trajectory Structure:
    - get_trajectory() returns a list of Tick objects
    - Each Tick contains all events from a single simulation step
    - Tool calls are queued and executed within the tick
    - Participants receive incoming chunks per tick
    - Use get_messages() to get a flat message list
    """

    def __init__(
        self,
        domain: str,
        agent: StreamingAgentT,
        user: StreamingUserT,
        environment: Environment,
        task: Task,
        max_steps: Optional[int] = 100,
        max_errors: int = 10,
        seed: Optional[int] = None,
        simulation_id: Optional[str] = None,
        tick_duration_seconds: Optional[float] = None,
        timeout: Optional[float] = None,
        call_context: Optional[CallContext] = None,
        watchdog_config=None,
        watchdog_judge=None,
    ):
        """
        Initialize FullDuplexOrchestrator.

        Args:
            domain: The domain name of the simulation.
            agent: The streaming agent instance (must have get_next_chunk method).
            user: The streaming user instance (must have get_next_chunk method).
            environment: The environment instance.
            task: The task specification.
            max_steps: Maximum number of simulation steps. None disables the limit.
            max_errors: Maximum number of tool execution errors.
            seed: Optional random seed for reproducibility.
            simulation_id: Optional simulation ID.
            tick_duration_seconds: Duration of each simulation tick in seconds (for timing metadata).
            timeout: Maximum wallclock time in seconds. None means no timeout.
            call_context: Resolved language, persona and scoring context.
        """
        super().__init__(
            domain=domain,
            agent=agent,
            user=user,
            environment=environment,
            task=task,
            max_steps=max_steps,
            max_errors=max_errors,
            seed=seed,
            simulation_id=simulation_id,
            timeout=timeout,
            call_context=call_context,
        )

        # Set mode to FULL_DUPLEX
        self.mode = CommunicationMode.FULL_DUPLEX

        # Full-duplex specific attributes
        self.current_user_chunk: Optional[UserMessage] = None
        self.current_agent_chunk: Optional[AssistantMessage] = None
        self.tick_duration_seconds = tick_duration_seconds
        self.watchdog = (
            WatchdogController(watchdog_config, judge=watchdog_judge)
            if watchdog_config is not None
            else None
        )
        self._watchdog_next_check = (
            watchdog_config.initial_check_ticks
            if watchdog_config is not None and watchdog_config.enabled
            else None
        )
        self._watchdog_stall_requested = False

        # Pending tool results: executed this tick, delivered to participant next tick.
        # This decouples tool execution from the audio/tick loop so that:
        # - No silence is injected during tool result delivery
        # - Agent audio from the tool-call tick is preserved
        # - User audio continuity is maintained
        self.pending_agent_tool_results: Optional[Message] = None
        self.pending_user_tool_results: Optional[Message] = None

        # Tick-based trajectory structure
        self.ticks: list[Tick] = []
        self._tool_executor = None
        self._scenario_user_input = None
        self._scenario_user_text = ""
        self._scenario_user_utterance = None
        self._scenario_last_user_time = None
        self._scenario_seen_fragments = set()
        self._scenario_audio_ms = [0.0]
        self._scenario_audio_times = [
            task.scenario.start_time if task.scenario else 0.0
        ]
        if task.scenario is not None:
            from tau2.environment.execution import ToolExecutionQueue

            self._tool_executor = ToolExecutionQueue(environment)

        # Validate mode compatibility
        self._validate_mode_compatibility()

    def _watchdog_stale(self) -> None:
        self._watchdog_stall_requested = True

    def _activate_observer_stage(self, user_chunk: UserMessage) -> None:
        """Activate staged observer vocabulary only when a request is spoken."""
        if not user_chunk.contains_speech:
            return
        raw = user_chunk.raw_data or {}
        event_id = raw.get("scenario_input_id")
        stage_ids = getattr(self.user, "scenario_dynamic_stage_ids", {})
        stage = stage_ids.get(event_id)
        scenario_stages = getattr(self, "scenario_stages", None)
        if stage is None and scenario_stages is not None:
            stage = scenario_stages.current
        setter = getattr(self.agent, "set_observer_stage", None)
        if setter is not None and stage is not None:
            setter(stage)

    def _watchdog_context(self) -> str:
        scenario = self.task.scenario
        series = self.scenario_stages
        stage = None
        task_text = (
            scenario.stages[series.current if series is not None else 0].request
            if scenario is not None and scenario.stages
            else str(self.task.description or "")
        )
        if scenario is not None and scenario.stages:
            current = series.current if series is not None else 0
            stage = {
                "current_stage": scenario.stages[current].stage,
                "total_stages": len(scenario.stages),
                "transition_pending": bool(series and series.transition_pending),
                "awaiting_delivery": bool(series and series.awaiting_delivery),
            }
        runtime = getattr(self.environment, "scenario_runtime", None)
        trace = (
            [record.model_dump(mode="json") for record in runtime.records]
            if runtime is not None
            else getattr(self.environment, "trace", [])
        )
        executor = self._tool_executor
        return compact_watchdog_context(
            task_text=task_text,
            ticks=self.ticks,
            environment_trace=trace,
            pending={
                "heartbeat_stale": self.watchdog.stale,
                "heartbeat_age_seconds": time.monotonic()
                - self.watchdog.last_heartbeat,
                "heartbeat_timeout_seconds": self.watchdog.config.heartbeat_timeout_seconds,
                "phase": self.watchdog.last_phase,
                "phase_age_seconds": time.monotonic() - self.watchdog.phase_started,
                "agent_has_pending_output": bool(
                    getattr(self.agent, "has_pending_output", False)
                ),
                "user_speech_active": bool(
                    self.current_user_chunk and is_speaking(self.current_user_chunk)
                ),
                "agent_speech_active": bool(
                    self.current_agent_chunk and is_speaking(self.current_agent_chunk)
                ),
                "termination_reason": (
                    self.termination_reason.value
                    if self.termination_reason is not None
                    else None
                ),
                "tool_executor_pending": list(executor.pending) if executor else [],
                "pending_agent_tool_results": bool(self.pending_agent_tool_results),
                "pending_user_tool_results": bool(self.pending_user_tool_results),
                "scenario_user_input": self._scenario_user_input,
            },
            context_ticks=self.watchdog.config.context_ticks,
            current_time=runtime.time() if runtime is not None else get_now(),
            wall_time=get_now(),
            stage=stage,
        )

    def _run_watchdog_checkpoint(self) -> None:
        if self.watchdog is None or not self.watchdog.config.enabled:
            return
        cap = self.watchdog.config.max_total_ticks
        if cap is not None and self.step_count >= cap:
            self.done = True
            self.termination_reason = TerminationReason.WATCHDOG_STALL
            return
        try:
            verdict = self.watchdog.judge(self._watchdog_context())
        except Exception as error:
            # An optional inspection must not discard the simulation evidence.
            logger.warning("Watchdog checkpoint unavailable: {}", type(error).__name__)
            verdict = WatchdogVerdict(
                decision="uncertain",
                state="unknown",
                confidence=0.0,
                reason=f"Watchdog checkpoint unavailable: {type(error).__name__}",
            )
        action = verdict.decision
        if verdict.decision == "stop_and_evaluate":
            self.done = True
            self.termination_reason = TerminationReason.WATCHDOG_STALL
            action = "stop_and_evaluate"
        self.watchdog.record(self.step_count, verdict, action)
        interval = self.watchdog.config.check_interval_ticks
        self._watchdog_next_check = (
            min(self.step_count + interval, cap) if cap else self.step_count + interval
        )

    def _validate_mode_compatibility(self):
        """Validate that agent and user support full-duplex streaming."""
        # Check if agent has get_next_chunk method
        if not hasattr(self.agent, "get_next_chunk"):
            raise ValueError(
                f"FULL_DUPLEX mode requires agent to have 'get_next_chunk' method. "
                f"Agent {self.agent.__class__.__name__} does not support streaming."
            )

        # Check if user has get_next_chunk method
        if not hasattr(self.user, "get_next_chunk"):
            raise ValueError(
                f"FULL_DUPLEX mode requires user to have 'get_next_chunk' method. "
                f"User {self.user.__class__.__name__} does not support streaming."
            )

        logger.info(
            f"FullDuplexOrchestrator initialized with "
            f"agent={self.agent.__class__.__name__}, "
            f"user={self.user.__class__.__name__}"
        )

    def initialize(self):
        """Initialize the orchestrator for full-duplex communication."""
        initial_state = self.task.initial_state
        initialization_data = (
            initial_state.initialization_data if initial_state is not None else None
        )
        initialization_actions = (
            initial_state.initialization_actions if initial_state is not None else None
        )
        message_history = (
            deepcopy(initial_state.message_history)
            if initial_state is not None and initial_state.message_history is not None
            else []
        )

        # Full-duplex doesn't support message history initialization yet
        if len(message_history) > 0:
            raise ValueError(
                "Full duplex mode does not yet support message history initialization"
            )

        # Initialize Environment state
        self._initialize_environment(
            initialization_data=initialization_data,
            initialization_actions=initialization_actions,
            message_history=message_history,
        )

        # Set seeds
        if self.seed is not None:
            self.agent.set_seed(self.seed)
            self.user.set_seed(self.seed)

        # Full-duplex specific initialization
        self._initialize_full_duplex()

        self.environment.sync_tools()

    def _initialize_full_duplex(self):
        """Initialize full-duplex specific state."""
        # Initialize user and agent states
        self.user_state = self.user.get_init_state()

        # Create first agent message
        # If agent has create_initial_message (audio-native agents), use it to get
        # a message with proper audio content. Otherwise, use the text-only default.
        if hasattr(self.agent, "create_initial_message"):
            opener = (
                self.task.agent_opener
                if self.task.agent_opener is not None
                else self.initial_agent_message.content
                if self.initial_agent_message is not None
                else None
            )
            first_agent_message = (
                self.agent.create_initial_message(content=opener)
                if opener is not None
                else self.agent.create_initial_message()
            )
        else:
            first_agent_message = deepcopy(
                self.initial_agent_message or DEFAULT_FIRST_AGENT_MESSAGE
            )
            if self.task.agent_opener is not None:
                first_agent_message.content = self.task.agent_opener
            first_agent_message.chunk_id = 0
            first_agent_message.is_final_chunk = True
            first_agent_message.timestamp = get_now()

        self.agent_state = self.agent.get_init_state(
            message_history=[first_agent_message]
        )

        # Initialize current message trackers with dummy message
        dummy_agent_message = AssistantMessage(
            role="assistant",
            content=None,
            timestamp=get_now(),
            chunk_id=0,
            is_final_chunk=True,
            contains_speech=False,
        )

        self.current_agent_chunk = first_agent_message

        # Get first user chunk
        self.current_user_chunk, self.user_state = self.user.get_next_chunk(
            self.user_state, participant_chunk=dummy_agent_message
        )

        diagnostic_recorder = getattr(self.environment, "diagnostic_recorder", None)
        if diagnostic_recorder is not None:
            if self.current_user_chunk is not None and self.current_user_chunk.content:
                diagnostic_recorder.message_event(
                    "user_turn",
                    "user",
                    self.current_user_chunk.content,
                    ["agent"],
                    event_id="tick:initial:user",
                )
            if first_agent_message is not None and first_agent_message.content:
                diagnostic_recorder.message_event(
                    "agent_turn",
                    "agent",
                    first_agent_message.content,
                    ["agent"],
                    event_id="tick:initial:agent",
                )

        # Initialize trajectory with first tick
        init_start_time = time.perf_counter()
        first_tick = Tick(
            tick_id=0,
            timestamp=get_now(),
            agent_chunk=deepcopy(self.current_agent_chunk),
            user_chunk=deepcopy(self.current_user_chunk),
            tick_duration_seconds=self.tick_duration_seconds,
        )
        first_tick.wall_clock_duration_seconds = time.perf_counter() - init_start_time
        runtime = getattr(self.environment, "scenario_runtime", None)
        if runtime is not None:
            first_tick.environment_events = [
                record.model_dump(mode="json") for record in runtime.drain()
            ]
        self.ticks = [first_tick]

    def _format_chunk_summary(self, chunk: Optional[Message], role: str) -> str:
        """Format a chunk for logging with key information."""
        if chunk is None:
            return f"{role}: (none)"

        parts = [f"{role}:"]

        # Content summary
        if chunk.content:
            content_preview = (
                chunk.content[:80] + "..." if len(chunk.content) > 80 else chunk.content
            )
            parts.append(f'  content="{content_preview}"')
        else:
            parts.append("  content=(empty)")

        # Speech indicator
        if hasattr(chunk, "contains_speech"):
            parts.append(f"  speech={chunk.contains_speech}")

        # Chunk metadata
        if hasattr(chunk, "chunk_id") and chunk.chunk_id is not None:
            parts.append(f"  chunk_id={chunk.chunk_id}")
        if hasattr(chunk, "is_final_chunk") and chunk.is_final_chunk is not None:
            parts.append(f"  is_final={chunk.is_final_chunk}")

        # Tool calls
        if hasattr(chunk, "tool_calls") and chunk.tool_calls:
            tool_names = [tc.name for tc in chunk.tool_calls]
            parts.append(f"  tool_calls={tool_names}")

        return " | ".join(parts)

    def step(self):
        """
        Perform one tick of full-duplex streaming communication.

        Both agent and user generate chunks simultaneously.
        Tool calls are executed within the tick but results are delivered
        on the next tick, keeping audio flow uninterrupted.
        All events are grouped into a single Tick object.
        """
        if self.done:
            raise ValueError("Simulation is done")

        if self.watchdog is not None and self.watchdog.config.enabled:
            self.watchdog.heartbeat(self.step_count, "tick")

        runtime = getattr(self.environment, "scenario_runtime", None)
        communication_busy = bool(
            getattr(self.agent, "has_pending_output", False)
            or is_speaking(self.current_agent_chunk)
            or (self.current_user_chunk and self.current_user_chunk.contains_speech)
            or self._scenario_user_input
            or (self._tool_executor and self._tool_executor.pending)
            or self.pending_agent_tool_results
            or self.pending_user_tool_results
        )
        runtime_managers = self._runtime_managers()
        if runtime is not None:
            for manager in runtime_managers:
                manager.advance(
                    runtime.time(),
                    not communication_busy
                    and (
                        self.async_evidence_source.get_status()["request_boundary"]
                        if getattr(self, "async_evidence_source", None) is not None
                        else True
                    ),
                )
        if self.scenario_stages is not None and self.scenario_stages.transition_pending:
            busy = bool(self._tool_executor.pending) if self._tool_executor else False
            busy = busy or bool(
                self.pending_agent_tool_results or self.pending_user_tool_results
            )
            busy = busy or getattr(self.agent, "has_pending_output", False)
            busy = busy or bool(is_speaking(self.current_agent_chunk))
            if not busy:
                self.scenario_stages.release(self.user, runtime, len(self.ticks))
        if runtime is not None:
            if not communication_busy:
                runtime.notify_turn_boundary()
            runtime.advance()
            with runtime.lock:
                while runtime.user_inputs:
                    key, text = runtime.user_inputs.pop(0)
                    self.user.queue_scenario_input(key, text)

        tick_id = len(self.ticks)
        logger.debug(f"Tick {tick_id}. step_count={self.step_count}")

        # Sanity check: stored chunks should never carry tool_calls
        # (tool_calls are stripped and tracked separately)
        if self.current_agent_chunk and self.current_agent_chunk.is_tool_call():
            raise ValueError(
                f"Agent chunk should not have tool_calls at step start: "
                f"{self.current_agent_chunk}"
            )
        if self.current_user_chunk and self.current_user_chunk.is_tool_call():
            raise ValueError(
                f"User chunk should not have tool_calls at step start: "
                f"{self.current_user_chunk}"
            )

        # Create new tick
        diagnostic_started = (
            time.monotonic()
            if getattr(self.environment, "diagnostic_recorder", None)
            else None
        )
        tick_start_time = time.perf_counter()
        tick = Tick(
            tick_id=tick_id,
            timestamp=get_now(),
            tick_duration_seconds=self.tick_duration_seconds,
        )

        # Both participants receive the other's previous chunk
        incoming_for_user = self.current_agent_chunk
        incoming_for_agent = self.current_user_chunk

        # Log incoming chunks from previous tick
        logger.debug(f"[Tick {tick_id}] Incoming chunks from previous tick:")
        logger.debug(
            f"  → User receives: {self._format_chunk_summary(incoming_for_user, 'agent_chunk')}"
        )
        logger.debug(
            f"  → Agent receives: {self._format_chunk_summary(incoming_for_agent, 'user_chunk')}"
        )

        # --- 1. Process user turn ---
        (
            user_chunk,
            self.user_state,
            user_tool_calls,
            user_tool_results,
        ) = self._process_participant_turn(
            participant=self.user,
            state=self.user_state,
            incoming_chunk=incoming_for_user,
            is_agent=False,
            pending_tool_results=self.pending_user_tool_results,
        )
        tick.user_chunk = deepcopy(user_chunk)
        tick.user_tool_calls = [deepcopy(tc) for tc in user_tool_calls]
        tick.user_tool_results = [deepcopy(r) for r in user_tool_results]
        self.pending_user_tool_results = (
            self._wrap_tool_results(user_tool_results) if user_tool_results else None
        )

        # --- 2. Process agent turn ---
        input_time = None
        foreground_origin = None
        if (
            runtime is not None
            and incoming_for_agent is not None
            and incoming_for_agent.contains_speech
        ):
            if getattr(self.user, "saasr_session", None) is not None:
                # A chunk can begin with background before caller speech starts.
                # Bind the initial input to its sample offset after transport.
                offset = (incoming_for_agent.raw_data or {}).get(
                    "saasr_speech_offset_seconds", 0
                )
                foreground_origin = runtime.monotonic() + offset
                if runtime.started:
                    input_time = runtime.timestamp_at(foreground_origin)
            else:
                runtime.start()
                input_time = runtime.time()
        diagnostic_recorder = getattr(self.environment, "diagnostic_recorder", None)
        diagnostic_media_count = (
            len(diagnostic_recorder.media_ticks) if diagnostic_recorder else 0
        )
        diagnostic_agent_start = time.monotonic() if diagnostic_recorder else None
        (
            agent_chunk,
            self.agent_state,
            agent_tool_calls,
            agent_tool_results,
        ) = self._process_participant_turn(
            participant=self.agent,
            state=self.agent_state,
            incoming_chunk=incoming_for_agent,
            is_agent=True,
            pending_tool_results=self.pending_agent_tool_results,
        )
        if (
            diagnostic_recorder is not None
            and len(diagnostic_recorder.media_ticks) == diagnostic_media_count
        ):
            diagnostic_recorder.media(
                incoming_for_agent,
                agent_chunk,
                diagnostic_agent_start,
                time.monotonic(),
                self.tick_duration_seconds,
            )
        tick.agent_chunk = deepcopy(agent_chunk)
        scene_audio = getattr(self.user, "saasr_session", None)
        if scene_audio is not None:
            scene_audio.record_sent(incoming_for_agent)
        if foreground_origin is not None and not runtime.started:
            runtime.start(monotonic_origin=min(foreground_origin, runtime.monotonic()))
            input_time = runtime.spec.start_time
        tick.agent_tool_calls = [deepcopy(tc) for tc in agent_tool_calls]
        tick.agent_tool_results = [deepcopy(r) for r in agent_tool_results]
        self.pending_agent_tool_results = (
            self._wrap_tool_results(agent_tool_results) if agent_tool_results else None
        )

        diagnostic_recorder = getattr(self.environment, "diagnostic_recorder", None)
        if diagnostic_recorder is not None:
            if (
                user_chunk is not None
                and user_chunk.content
                and user_chunk.is_final_chunk
            ):
                diagnostic_recorder.message_event(
                    "user_turn",
                    "user",
                    user_chunk.content,
                    ["agent"],
                    event_id=f"tick:{tick_id}:user",
                )
            if (
                agent_chunk is not None
                and agent_chunk.content
                and agent_chunk.is_final_chunk
            ):
                diagnostic_recorder.message_event(
                    "agent_turn",
                    "agent",
                    agent_chunk.content,
                    ["agent"],
                    event_id=f"tick:{tick_id}:agent",
                )

        # --- 3. Update state and bookkeeping ---
        self.current_user_chunk = user_chunk
        self.current_agent_chunk = agent_chunk
        if runtime is not None:
            self._observe_scenario_delivery(
                runtime, incoming_for_agent, agent_chunk, input_time=input_time
            )
            tick.environment_events = [
                record.model_dump(mode="json") for record in runtime.drain()
            ]

        # Record wall clock duration for this tick
        tick.wall_clock_duration_seconds = time.perf_counter() - tick_start_time

        logger.debug(
            f"[Tick {tick_id}] Wall-clock duration: "
            f"{tick.wall_clock_duration_seconds:.3f}s"
        )
        if (
            self.tick_duration_seconds is not None
            and tick.wall_clock_duration_seconds < self.tick_duration_seconds
        ):
            logger.warning(
                f"[Tick {tick_id}] Completed in "
                f"{tick.wall_clock_duration_seconds:.3f}s, which is less than "
                f"the expected tick duration of {self.tick_duration_seconds:.3f}s. "
                f"Wall-clock pacing may not be enforced by the adapter."
            )

        if diagnostic_started is not None:
            timeline = getattr(self.environment, "_diagnostic_media_timeline", None)
            if timeline is None:
                timeline = self.environment._diagnostic_media_timeline = {}
            timeline[tick.tick_id] = (diagnostic_started, time.monotonic())
        self.ticks.append(tick)

        self.step_count += 1
        self.environment.sync_tools()

    def _process_participant_turn(
        self,
        participant: Union[StreamingAgentT, StreamingUserT],
        state: Any,
        incoming_chunk: Optional[Message],
        is_agent: bool,
        pending_tool_results: Optional[Message] = None,
    ) -> tuple[Message, Any, list[ToolCall], list[ToolMessage]]:
        """
        Process a participant's turn with dual-channel input.

        The participant receives both channels in a single get_next_chunk call:
        - participant_chunk: speech/audio from the other participant
        - tool_results: results from previously executed tool calls (if any)

        If the participant's response contains tool calls, they are executed
        immediately. The caller is responsible for wrapping them via
        _wrap_tool_results and storing as pending for the next tick.

        Args:
            participant: The agent or user instance.
            state: The current state of the participant.
            incoming_chunk: The incoming message chunk from the other participant.
            is_agent: True if processing agent, False if processing user.
            pending_tool_results: Tool results from previous tick to deliver.

        Returns:
            Tuple of (chunk, new_state, tool_calls, tool_results).
        """
        participant_name = "AGENT" if is_agent else "USER"

        is_stop = self.agent.is_stop if is_agent else UserSimulator.is_stop
        termination_reason = (
            TerminationReason.AGENT_STOP if is_agent else TerminationReason.USER_STOP
        )

        new_state = state
        if pending_tool_results is not None:
            logger.debug(
                f"  [{participant_name}] Delivering tool results alongside "
                f"participant chunk"
            )

        # Single get_next_chunk call with both channels
        logger.debug(f"  [{participant_name}] Calling get_next_chunk...")
        scenario_runtime = getattr(self.environment, "scenario_runtime", None)
        pause_for_first_saasr_audio = bool(
            not is_agent
            and scenario_runtime is not None
            and getattr(participant, "saasr_session", None) is not None
            and not getattr(participant, "realtime_generation", True)
        )
        if pause_for_first_saasr_audio:
            scenario_runtime.pause()
        try:
            new_chunk, new_state = participant.get_next_chunk(
                state=new_state,
                participant_chunk=incoming_chunk,
                tool_results=pending_tool_results,
            )
        except (UserError, SaasrWorkerError) as error:
            if is_agent:
                raise
            self.done = True
            if isinstance(error, SaasrWorkerError):
                self.termination_reason = TerminationReason.INFRASTRUCTURE_ERROR
                self._audio_renderer_error = str(error)
            else:
                self.termination_reason = TerminationReason.USER_ERROR
                self._user_error = str(error)
            return (
                UserMessage(role="user", content=None, contains_speech=False),
                new_state,
                [],
                [],
            )
        finally:
            if pause_for_first_saasr_audio:
                scenario_runtime.resume()
        logger.debug(
            f"  [{participant_name}] Returned chunk: "
            f"{self._format_chunk_summary(new_chunk, 'chunk')}"
        )

        # Validate and check stop condition
        if new_chunk.contains_speech:
            new_chunk.validate()
        if (
            not is_agent
            and self.scenario_stages is not None
            and self.scenario_stages.stop(
                new_chunk, self.environment.scenario_runtime, len(self.ticks)
            )
        ):
            self.user.scenario_transition_pending = (
                self.scenario_stages.transition_pending
            )
            new_chunk = UserMessage(
                role="user",
                content=None,
                contains_speech=False,
                cost=new_chunk.cost,
                usage=new_chunk.usage,
            )
        if is_stop(new_chunk):
            logger.info(f"  [{participant_name}] *** STOP signal detected ***")
            self.done = True
            self.termination_reason = termination_reason

        # Handle tool calls: execute now, deliver results next tick
        tool_calls: list[ToolCall] = []
        tool_results: list[ToolMessage] = []

        if new_chunk and new_chunk.is_tool_call():
            tool_calls = list(new_chunk.tool_calls)
            tool_names = [tc.name for tc in tool_calls]
            logger.info(f"  [{participant_name}] Tool calls: {tool_names}")

            if self._tool_executor is not None:
                self._tool_executor.submit(tool_calls)
                results = []
            else:
                results = self._execute_tool_calls(tool_calls)
            tool_results = list(results)

            if is_agent and hasattr(self.user, "observe_agent_tool_calls"):
                self.user.observe_agent_tool_calls(tool_calls)

            for tc, result in zip(tool_calls, results):
                result_preview = (
                    result.content[:100] + "..."
                    if len(result.content) > 100
                    else result.content
                )
                logger.debug(f"  [{participant_name}]   {tc.name}() → {result_preview}")

            # Return a copy with tool_calls stripped so the chunk only carries
            # speech/audio for the other participant. The original chunk (stored
            # in the participant's tick history via record_tick) must keep its
            # tool_calls intact for linearization / tool-result pairing.
            new_chunk = deepcopy(new_chunk)
            new_chunk.tool_calls = None

            logger.info(
                f"  [{participant_name}] Executed {len(tool_calls)} tool(s), "
                f"results pending for next tick"
            )

        if self._tool_executor is not None:
            ready = self._tool_executor.drain("assistant" if is_agent else "user")
            self.num_errors += sum(result.error for result in ready)
            tool_results.extend(ready)
        return new_chunk, new_state, tool_calls, tool_results

    def _observe_scenario_delivery(
        self, runtime, user_chunk, agent_chunk, *, input_time=None
    ):
        """Journal transported user input and actual playback, preserving both clocks."""
        if user_chunk is not None:
            utterance = tuple(user_chunk.utterance_ids or [])
            action = user_chunk.turn_taking_action
            if (
                self.scenario_stages is not None
                and self._scenario_user_input
                and action is not None
                and action.action == "stop_talking"
            ):
                retry = self.scenario_stages.interrupted(
                    self._scenario_user_input, runtime, len(self.ticks)
                )
                if retry is not None:
                    self.user.queue_scenario_input(retry.id, retry.arguments["content"])
                self._scenario_user_input = None
                self._scenario_user_text = ""
                self._scenario_user_utterance = None
            if (
                utterance
                and self._scenario_user_utterance
                and utterance != self._scenario_user_utterance
            ):
                if self._scenario_user_text and runtime.started:
                    runtime.delivered(
                        "user",
                        self._scenario_user_text,
                        delivered_at=self._scenario_last_user_time,
                    )
                if self.scenario_stages is not None and self._scenario_user_input:
                    retry = self.scenario_stages.interrupted(
                        self._scenario_user_input, runtime, len(self.ticks)
                    )
                    if retry is not None:
                        self.user.queue_scenario_input(
                            retry.id, retry.arguments["content"]
                        )
                self._scenario_user_text = ""
                self._scenario_user_input = None
            if utterance:
                self._scenario_user_utterance = utterance
            data = user_chunk.raw_data or {}
            self._scenario_user_input = data.get(
                "scenario_input_id", self._scenario_user_input
            )
            self._scenario_user_text += user_chunk.content or ""
            recorded = False
            if user_chunk.contains_speech:
                self._activate_observer_stage(user_chunk)
                if self.scenario_stages is not None and self._scenario_user_input:
                    self.scenario_stages.delivered(
                        self._scenario_user_input,
                        runtime,
                        len(self.ticks),
                        complete=bool(
                            user_chunk.is_final_chunk or data.get("saasr_main_final")
                        ),
                        when=input_time,
                    )
                self._scenario_last_user_time = runtime.time()
                if (
                    self._scenario_user_input
                    and self._scenario_user_input not in runtime.completed
                ):
                    runtime.delivered(
                        "user",
                        self._scenario_user_text,
                        event_id=self._scenario_user_input,
                        delivered_at=input_time,
                    )
                    recorded = True
                if user_chunk.is_final_chunk or data.get("saasr_main_final"):
                    if self._scenario_user_input:
                        for manager in self._runtime_managers():
                            manager.delivery_completed(
                                self._scenario_user_input, runtime.time()
                            )
                    if not recorded:
                        runtime.delivered("user", self._scenario_user_text)
                    self._scenario_user_input = None
                    self._scenario_user_text = ""
                    self._scenario_user_utterance = None
        raw = (agent_chunk.raw_data or {}) if agent_chunk is not None else {}
        stream_start = raw.get("played_stream_start_ms")
        stream_duration = raw.get("played_stream_duration_ms")
        now = raw.get("media_cutoff") or runtime.monotonic()
        if len(self._scenario_audio_ms) == 1:
            self._scenario_audio_times[0] = (
                raw.get("playback_start_monotonic")
                if raw.get("playback_start_monotonic") is not None
                else now - self.tick_duration_seconds
            )
        # Observer offsets refer to the cumulative audio actually presented by
        # the adapter. A continuous-media snapshot may contain more than one
        # nominal tick, especially after a synchronous caller-generation wait.
        self._scenario_audio_ms.append(
            stream_start + stream_duration
            if stream_start is not None and stream_duration is not None
            else self._scenario_audio_ms[-1] + self.tick_duration_seconds * 1000
        )
        self._scenario_audio_times.append(now)
        if agent_chunk is None or not runtime.started:
            return
        live = (agent_chunk.raw_data or {}).get("live_transcript", {})
        if (
            live.get("origin") != "recorded_audio"
            or live.get("alignment") != "played_audio"
        ):
            return
        for fragment in live.get("fragments", []):
            key = fragment.get("event_id") or fragment.get("id") or str(fragment)
            if key in self._scenario_seen_fragments:
                continue
            self._scenario_seen_fragments.add(key)
            text = fragment.get("text") or fragment.get("content")
            if not text:
                continue
            ended = fragment.get("end_ms")
            delivered_at = None
            if ended is not None:
                if ended > self._scenario_audio_ms[-1] + 1:
                    raise ValueError(
                        "Observer transcript refers to audio not yet played"
                    )
                index = min(
                    bisect_left(self._scenario_audio_ms, ended),
                    len(self._scenario_audio_ms) - 1,
                )
                if index:
                    fraction = (ended - self._scenario_audio_ms[index - 1]) / (
                        self._scenario_audio_ms[index]
                        - self._scenario_audio_ms[index - 1]
                    )
                    wall = self._scenario_audio_times[index - 1] + fraction * (
                        self._scenario_audio_times[index]
                        - self._scenario_audio_times[index - 1]
                    )
                    delivered_at = runtime.timestamp_at(wall)
            runtime.delivered("agent", text, delivered_at=delivered_at)

    def get_trajectory(self) -> list[Tick]:
        """
        Get the tick-based trajectory of the simulation.

        Returns:
            List of Tick objects, each containing all events from a simulation tick.
        """
        return deepcopy(self.ticks)

    def get_ticks(self) -> list[Tick]:
        """
        Get the tick-based trajectory.

        Alias for get_trajectory() for backward compatibility.

        Returns:
            List of Tick objects, each containing all events from a simulation tick.
        """
        return self.get_trajectory()

    def get_messages(self) -> list[Message]:
        """
        Get all messages from the simulation as a flat list.

        Converts the tick-based trajectory to a flat message list,
        sorted by timestamp with turn_idx assigned.

        Returns:
            List of all messages sorted by timestamp with turn_idx assigned.
        """
        messages: list[Message] = []
        for tick in self.ticks:
            messages.extend(tick.get_all_messages())
        messages = sorted(messages, key=lambda m: m.timestamp)
        result = []
        for i, msg in enumerate(messages):
            msg = deepcopy(msg)
            msg.turn_idx = i
            result.append(msg)
        return result

    def _check_termination(self) -> None:
        """
        Check for full-duplex specific termination conditions.

        Checks max_steps, max_errors, and timeout after each tick.
        """
        if self.done:
            return
        if self.max_steps is not None and self.step_count >= self.max_steps:
            extend_reply = False
            runtime = getattr(
                getattr(self, "environment", None), "scenario_runtime", None
            )
            if runtime is not None:
                for manager in self._runtime_managers():
                    if manager.should_extend_reply(runtime.time()):
                        manager.consume_reply_step(runtime.time())
                        extend_reply = True
                        break
            if not extend_reply:
                self.done = True
                self.termination_reason = TerminationReason.MAX_STEPS
        if self.num_errors >= self.max_errors:
            self.done = True
            self.termination_reason = TerminationReason.TOO_MANY_ERRORS
        self._check_timeout()
        if self.done:
            return
        if self.watchdog is not None and self.watchdog.config.enabled:
            self.watchdog.heartbeat(self.step_count, "termination_check")
            if self._watchdog_stall_requested:
                self.done = True
                self.termination_reason = TerminationReason.WATCHDOG_STALL
            elif (
                self._watchdog_next_check is not None
                and self.step_count >= self._watchdog_next_check
            ):
                self._run_watchdog_checkpoint()
        self._check_timeout()

    def _finalize(self) -> SimulationRun:
        """
        Finalize the full-duplex simulation and create the SimulationRun result.

        Sends stop signals to agent and user, calculates costs, and builds the result.

        Returns:
            SimulationRun with all simulation data.
        """
        # Send stop signals to agent and user, forwarding any pending tool results
        cancel_generation = getattr(self.user, "cancel_pending_generation", None)
        if cancel_generation is not None:
            cancel_generation()
        try:
            self.agent.stop(
                None, self.agent_state, tool_results=self.pending_agent_tool_results
            )
        except Exception as e:
            logger.warning(f"Error stopping agent during finalization: {e}")
        try:
            self.user.stop(
                None, self.user_state, tool_results=self.pending_user_tool_results
            )
        except Exception as e:
            logger.warning(f"Error stopping user during finalization: {e}")

        # Calculate duration
        duration = time.perf_counter() - self._run_start_perf

        # Derive messages for cost computation (not stored in SimulationRun)
        ticks = self.get_trajectory()
        final_live_transcript = None
        final_live_transcript_error = None
        get_final_live_transcript = getattr(
            self.agent, "get_final_live_transcript", None
        )
        if get_final_live_transcript is not None:
            final_live_transcript, final_live_transcript_error = (
                get_final_live_transcript()
            )
        _merge_final_live_transcript(ticks, final_live_transcript)
        agent_cost, user_cost = get_cost(self.get_messages())

        # Provider usage ledger (audio-native agents). The session-level
        # aggregation is the authoritative agent cost: it correctly handles
        # cumulative meters (Nova running totals, xAI audio-minutes, STT)
        # that per-message costs cannot represent.
        agent_usage = None
        if hasattr(self.agent, "get_usage_records"):
            usage_records = self.agent.get_usage_records()
            if usage_records:
                agent_usage = build_session_usage(usage_records)
                # Authoritative even when None: per-message sums exclude
                # cumulative meters, so falling back to them would report a
                # misleading $0 (xAI) or partial sum (LiveKit) for a session
                # that is actually unpriced.
                agent_cost = agent_usage.cost
                logger.info(
                    f"Agent usage: {agent_usage.num_raw_records} records, "
                    f"cost={agent_usage.cost} ({agent_usage.cost_breakdown})"
                )

        # Get speech_environment from user's voice_settings if available
        speech_environment = None
        if (
            hasattr(self.user, "voice_settings")
            and self.user.voice_settings is not None
        ):
            speech_environment = self.user.voice_settings.speech_environment

        # Compute responsiveness metrics
        info = compute_responsiveness_info(ticks)
        if self.watchdog is not None and self.watchdog.config.enabled:
            info["watchdog"] = self.watchdog.summary()
        scene_audio = getattr(self.user, "saasr_session", None)
        if scene_audio is not None:
            info["user_audio_renderer"] = scene_audio.record()
        if getattr(self, "_audio_renderer_error", None):
            info["audio_renderer_error"] = self._audio_renderer_error
        if getattr(self, "_user_error", None):
            info["user_simulator_error"] = self._user_error
        if final_live_transcript_error:
            info["final_live_transcript_error"] = final_live_transcript_error
        if hasattr(self.user, "get_generation_diagnostics"):
            diagnostics = self.user.get_generation_diagnostics()
            if diagnostics is not None:
                info["user_generation"] = diagnostics
                additional = diagnostics["additional_llm_cost"]
                user_cost = (
                    user_cost + additional
                    if user_cost is not None and additional is not None
                    else None
                )

        hidden_intent = None
        if hasattr(self.user, "get_hidden_intent_metrics"):
            hidden_intent = self.user.get_hidden_intent_metrics()
            if hidden_intent is not None:
                info["hidden_intent"] = hidden_intent

        # Extract provider session ID if available (e.g., OpenAI session ID)
        provider_session_id = None
        if hasattr(self.agent, "adapter") and hasattr(self.agent.adapter, "provider"):
            provider = self.agent.adapter.provider
            provider_session_id = getattr(provider, "session_id", None)

        # Collect effect timeline from user simulator if available
        effect_timeline = None
        if hasattr(self.user, "get_effect_timeline"):
            effect_timeline = self.user.get_effect_timeline()
            if effect_timeline and effect_timeline.events:
                logger.info(
                    f"Effect timeline: {len(effect_timeline.events)} events recorded"
                )

        simulation_run = SimulationRun(
            id=self.simulation_id,
            task_id=self.task.id,
            start_time=self._run_start_time,
            end_time=get_now(),
            duration=duration,
            termination_reason=self.termination_reason.value,
            reward_info=None,
            user_cost=user_cost,
            agent_cost=agent_cost,
            agent_usage=agent_usage,
            ticks=ticks,
            seed=self.seed,
            mode=self.mode.value,
            speech_environment=speech_environment,
            info=info,
            provider_session_id=provider_session_id,
            effect_timeline=effect_timeline,
        )
        return simulation_run

    def run(self) -> SimulationRun:
        """
        Run the simulation with post-processing for user transcripts.

        Overrides the base class run() to track timing and add transcript processing.

        Returns:
            SimulationRun: The simulation run.
        """
        if self.watchdog is not None and self.watchdog.config.enabled:
            self.watchdog.heartbeat(0, "run_start")
            self.watchdog.start(self._watchdog_stale)
        try:
            result = super().run()
        finally:
            if self.watchdog is not None and self.watchdog.config.enabled:
                self.watchdog.stop()
        compute_proportional_user_transcripts(self.ticks)
        return result

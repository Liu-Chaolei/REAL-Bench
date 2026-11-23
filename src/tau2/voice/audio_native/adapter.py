"""Abstract base classes and factory for audio native adapters.
DiscreteTimeAdapter: Tick-based pattern for discrete-time simulation.
   - run_tick() as the primary method
   - Audio time is the primary clock
   - Used by DiscreteTimeAudioNativeAgent

create_adapter(): Factory function that validates parameters and constructs
   the appropriate adapter subclass for a given provider.
"""

import asyncio
import base64
import inspect
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional, Tuple

from loguru import logger

from tau2.config import (
    DEFAULT_AUDIO_NATIVE_MODELS,
    DEFAULT_AUDIO_NATIVE_REASONING_EFFORT,
    DEFAULT_AUDIO_NATIVE_VOIP_PACKET_INTERVAL_MS,
    DEFAULT_SEND_AUDIO_INSTANT,
    TELEPHONY_ULAW_SILENCE,
)
from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT, AudioEncoding, AudioFormat
from tau2.data_model.usage import UsageRecord
from tau2.environment.tool import Tool
from tau2.voice.audio_activity import CONFIG as ACTIVITY_CONFIG
from tau2.voice.audio_activity import USER_EVENT_VERSION, MediaPlayback, SpeechDetector
from tau2.voice.audio_native.tick_result import (
    TickResult,
    UtteranceTranscript,
    get_proportional_transcript,
)

if TYPE_CHECKING:
    from tau2.data_model.live_config import LiveConfig
    from tau2.voice.audio_native.avartha_pipecat.config import AvarthaPipecatConfig
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig


class DiscreteTimeAdapter(ABC):
    """Abstract base class for discrete-time audio native adapters.

    Tasks retain a run_tick() interface. Media receipt, response-scoped playback
    and cancellation run continuously on the background event loop. Tick outputs
    are views of actually presented samples, not fixed-size audio caps.

    Attributes:
        tick_duration_ms: Duration of each tick in milliseconds.
        audio_format: Audio format for the external interface (user audio in,
            agent audio out). Defaults to telephony (8kHz μ-law).
        bytes_per_tick: Maximum agent audio bytes per tick, derived from
            tick_duration_ms and audio_format.

    Usage:
        adapter = SomeAdapter(tick_duration_ms=200)
        adapter.connect(system_prompt, tools, vad_config, modality="audio")

        for tick in range(max_ticks):
            result = adapter.run_tick(user_audio_bytes, tick_number=tick)
            # result.get_played_agent_audio() - actual presentation view
            # result.agent_audio_data - raw audio (for speech detection)
            # result.proportional_transcript - text for this tick
            # result.events - all API events received

        adapter.disconnect()
    """

    def __init__(
        self,
        tick_duration_ms: int,
        audio_format: Optional[AudioFormat] = None,
        send_audio_instant: bool = DEFAULT_SEND_AUDIO_INSTANT,
    ):
        """Initialize the adapter.

        Args:
            tick_duration_ms: Duration of each tick in milliseconds. Must be > 0.
            audio_format: Audio format for the external interface. Defaults to
                telephony (8kHz μ-law). Subclasses may pass a different format
                if their provider uses a non-telephony external format.
            send_audio_instant: If True, send all audio in one call per tick.
                If False, send in VoIP-style 20ms chunks with sleeps.

        Raises:
            ValueError: If tick_duration_ms is <= 0.
        """
        if tick_duration_ms <= 0:
            raise ValueError(f"tick_duration_ms must be > 0, got {tick_duration_ms}")

        self.tick_duration_ms = tick_duration_ms
        self.audio_format = audio_format or TELEPHONY_AUDIO_FORMAT
        self.bytes_per_tick = int(
            self.audio_format.bytes_per_second * tick_duration_ms / 1000
        )
        self.send_audio_instant = send_audio_instant
        self.realtime_pacing = False
        self._next_tick_start: float | None = None
        self.playback = MediaPlayback(self.audio_format)
        self._media_task = None
        self._playback_task = None
        self._stream_result = None
        self._media_rows = []
        self._media_origin = None
        self._played_stream_ms = 0.0
        self._input_context = None
        self._speech_detector = SpeechDetector(self.audio_format.sample_rate)
        self._speech_origin = None
        self._speech_end = None
        self._speech_intervals = []
        self._voip_interval_ms = DEFAULT_AUDIO_NATIVE_VOIP_PACKET_INTERVAL_MS

        # Shared tick state (managed by _async_run_tick template)
        self._tick_count: int = 0
        self._cumulative_user_audio_ms: int = 0
        self._buffered_agent_audio: List[Tuple[bytes, Optional[str]]] = []
        self._utterance_transcripts: dict[str, UtteranceTranscript] = {}
        self._current_item_id: Optional[str] = None
        self._skip_item_id: Optional[str] = None
        self._pending_tool_results: List[Tuple] = []

        # Optional item-ID mapping (used by Nova where audio and text have
        # different content IDs). Subclasses that need it should populate this
        # dict; get_proportional_transcript will forward it automatically.
        self._item_id_map: Optional[dict[str, str]] = None

        # Usage ledger. Survives disconnect() (which only clears tick buffers)
        # so the orchestrator can collect usage after the agent is stopped.
        self._usage_records: List[UsageRecord] = []
        self._tick_usage_buffer: List[UsageRecord] = []
        self._current_tick_number: Optional[int] = None

    @abstractmethod
    def connect(
        self,
        system_prompt: str,
        tools: List[Tool],
        vad_config: Any,
        modality: str = "audio",
    ) -> None:
        """Connect to the API and configure the session.

        Args:
            system_prompt: System prompt for the agent.
            tools: List of tools the agent can use.
            vad_config: VAD configuration (e.g., OpenAIVADConfig).
            modality: "audio" for full audio, "audio_in_text_out" for audio input only.
        """
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect from the API and clean up resources."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if the adapter is connected to the API."""
        raise NotImplementedError

    @abstractmethod
    def run_tick(
        self,
        user_audio: bytes,
        tick_number: Optional[int] = None,
    ) -> TickResult:
        """Run one tick of the simulation.

        Subclasses implement this to bridge sync/async (e.g., via BackgroundAsyncLoop).
        The async work should delegate to _async_run_tick() which provides the
        shared tick lifecycle (buffering, transcript, etc.).

        Args:
            user_audio: User audio bytes for this tick (in audio_format encoding).
            tick_number: Optional tick number for logging/tracking.

        Returns:
            TickResult with capped audio, proportional transcript, events.
        """
        raise NotImplementedError

    def send_tool_result(
        self,
        call_id: str,
        result: str,
        request_response: bool = True,
        is_error: bool = False,
    ) -> None:
        """Queue a tool result to be sent in the next tick."""
        self._pending_tool_results.append((call_id, result, request_response, is_error))
        logger.debug(f"Queued tool result for call_id={call_id}")

    def clear_buffers(self) -> None:
        """Reset all internal tick state.

        Note: the usage ledger is deliberately NOT cleared — usage must
        survive disconnect() so it can be collected at simulation end.
        """
        if self._media_task is not None:
            task, self._media_task = self._media_task, None
            if not task.done() and task.get_loop().is_running():
                task.get_loop().call_soon_threadsafe(task.cancel)
            elif task.done() and not task.cancelled():
                task.exception()
        if self._playback_task is not None:
            task, self._playback_task = self._playback_task, None
            if not task.done() and task.get_loop().is_running():
                task.get_loop().call_soon_threadsafe(task.cancel)
            elif task.done() and not task.cancelled():
                task.exception()
        self._stream_result = None
        self.playback = MediaPlayback(self.audio_format)
        self._media_rows = []
        self._media_origin = None
        self._played_stream_ms = 0.0
        self._speech_detector = SpeechDetector(self.audio_format.sample_rate)
        self._speech_origin = self._speech_end = None
        self._speech_intervals = []
        self._buffered_agent_audio.clear()
        self._next_tick_start = None
        self._utterance_transcripts.clear()
        self._pending_tool_results.clear()
        self._skip_item_id = None

    # -----------------------------------------------------------------------
    # Usage tracking
    # -----------------------------------------------------------------------

    def record_usage(self, record: UsageRecord) -> None:
        """Record a usage observation from the provider.

        Appends to the session ledger (returned by get_usage_records) and to
        the current tick's buffer (drained into TickResult.usage_records).
        """
        if record.tick_number is None:
            record.tick_number = self._current_tick_number
        self._usage_records.append(record)
        self._tick_usage_buffer.append(record)

    def get_usage_records(self) -> List[UsageRecord]:
        """All usage records collected over the adapter's lifetime.

        Safe to call after disconnect(). Subclasses may override to append
        live counters (e.g. LiveKit's cumulative STT audio meter).
        """
        return list(self._usage_records)

    # -----------------------------------------------------------------------
    # Tick lifecycle template
    # -----------------------------------------------------------------------

    async def _async_run_tick(self, user_audio: bytes, tick_number: int) -> TickResult:
        """Template method for the tick lifecycle.

        Send the next user input while the persistent receiver continues processing
        protocol events. Snapshot actually played samples and precise timestamps;
        leave future audio in the shared presentation queue.
        """
        self._media_loop = asyncio.get_running_loop()
        tick_start = time.monotonic()
        if self._media_origin is None:
            self._media_origin = tick_start
        self._current_tick_number = tick_number
        pending_delivery_ids = [row[0] for row in self._pending_tool_results]
        await self._flush_pending_tool_results()
        recorder = getattr(self, "diagnostic_recorder", None)
        if recorder is not None:
            for call_id in pending_delivery_ids:
                recorder.tool_delivered(call_id, viewer="backend")
        if self._stream_result is None:
            self._stream_result = TickResult(
                tick_number=tick_number,
                audio_sent_bytes=len(user_audio),
                audio_sent_duration_ms=len(user_audio)
                / self.audio_format.bytes_per_second
                * 1000,
                bytes_per_tick=self.bytes_per_tick,
                bytes_per_second=self.audio_format.bytes_per_second,
                silence_byte=TELEPHONY_ULAW_SILENCE,
            )
        live = self._stream_result
        live.tick_number = tick_number
        live.cumulative_user_audio_at_tick_start_ms = self._cumulative_user_audio_ms
        live.audio_sent_bytes = len(user_audio)
        live.audio_sent_duration_ms = (
            len(user_audio) / self.audio_format.bytes_per_second * 1000
        )
        live.user_audio_data = user_audio
        if self._playback_task is None:
            self._playback_task = asyncio.create_task(self._pace_playback())
        await self._execute_tick(user_audio, tick_number, live, tick_start)
        remaining = self.tick_duration_ms / 1000 - (time.monotonic() - tick_start)
        if remaining > 0:
            await asyncio.sleep(remaining)
        for task in (self._media_task, self._playback_task):
            if task is not None and task.done():
                task.result()
        ended = time.monotonic()
        self._drain_playback(ended)
        rows, self._media_rows = self._media_rows, []
        result = live.model_copy(deep=True)
        result.media_timeline = rows
        result.media_origin = self._media_origin
        result.media_cutoff = ended
        result.speech_config = dict(ACTIVITY_CONFIG)
        result.speech_intervals = [list(r) for r in self._speech_intervals]
        self._speech_intervals = []
        result.contains_speech = bool(result.speech_intervals)
        # Compatibility audio is a presentation view, including leading silence.
        output_rows = [r for r in rows if r["role"] == "agent"]
        start = min([tick_start] + [r["start"] for r in output_rows])
        count = round((ended - start) * self.audio_format.bytes_per_second)
        width = self.audio_format.bytes_per_sample
        count -= count % width
        silence = {
            AudioEncoding.ULAW: TELEPHONY_ULAW_SILENCE,
            AudioEncoding.ALAW: b"\xd5",
            AudioEncoding.PCM_U8: b"\x80",
        }.get(
            self.audio_format.encoding, b"\0" * self.audio_format.sample_width
        ) * self.audio_format.channels
        output = bytearray(silence * (count // width))
        result.agent_audio_chunks = []
        for row in output_rows:
            data = base64.b64decode(row["audio"])
            offset = round((row["start"] - start) * self.audio_format.bytes_per_second)
            offset -= offset % width
            output[offset : offset + len(data)] = data
            result.agent_audio_chunks.append((data, row.get("response_id")))
        result.played_audio = bytes(output)
        result.playback_start_monotonic = start
        result.played_stream_start_ms = self._played_stream_ms
        result.played_stream_duration_ms = (
            len(output) / self.audio_format.bytes_per_second * 1000
        )
        self._played_stream_ms += result.played_stream_duration_ms
        result.speaking_now = bool(
            result.speech_intervals
            and result.speech_intervals[-1][1]
            >= ended - ACTIVITY_CONFIG["frame_ms"] / 1000
        )
        result.proportional_transcript = get_proportional_transcript(
            result.agent_audio_chunks,
            self._utterance_transcripts,
            item_id_map=self._item_id_map,
        )
        result.tick_sim_duration_ms = (ended - tick_start) * 1000
        result.usage_records = list(self._tick_usage_buffer)
        self._tick_usage_buffer.clear()
        self._cumulative_user_audio_ms += int(live.audio_sent_duration_ms)
        for field in (
            "events",
            "vad_events",
            "tool_calls",
            "generated_text",
            "media_tool_calls",
        ):
            setattr(live, field, [])
        live.playback_cancellations = None
        live.was_truncated = False
        live.truncated_audio_bytes = 0
        return result

    async def _pace_playback(self):
        """Advance detection and playback acknowledgements independently of ticks."""
        while True:
            await asyncio.sleep(ACTIVITY_CONFIG["frame_ms"] / 1000)
            self._drain_playback()
            await self._playback_progress()

    async def _playback_progress(self):
        """Optional protocol receipts for actually presented samples."""

    def finish_media(self):
        """Stop presentation at call end and return any samples played between ticks."""
        loop = getattr(self, "_media_loop", None)
        if loop is None or not loop.is_running() or self._stream_result is None:
            return None
        return asyncio.run_coroutine_threadsafe(self._finish_media(), loop).result(
            timeout=10
        )

    async def _finish_media(self):
        task, self._media_task = self._media_task, None
        if task is not None:
            if task.done():
                task.exception() if not task.cancelled() else None
            else:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        task, self._playback_task = self._playback_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        ended = time.monotonic()
        self._drain_playback(ended)
        self.playback.cancel(at=ended)
        # Flush the last partial detection window at the actual playback cutoff.
        self._drain_playback(ended)
        rows, self._media_rows = self._media_rows, []
        intervals, self._speech_intervals = self._speech_intervals, []
        return dict(media_timeline=rows, speech_intervals=intervals, media_cutoff=ended)

    def _drain_playback(self, until=None):
        from tau2.data_model.audio import AudioData
        from tau2.voice.utils.audio_preprocessing import convert_to_pcm16

        for row in self.playback.drain(until):
            pcm = convert_to_pcm16(
                AudioData(data=base64.b64decode(row["audio"]), format=self.audio_format)
            ).data
            if self.audio_format.channels == 2:
                import audioop

                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            if (
                self._speech_end is None
                or abs(row["start"] - self._speech_end)
                > 1 / self.audio_format.sample_rate
            ):
                if self._speech_origin is not None:
                    self._speech_intervals.extend(
                        [
                            [self._speech_origin + a, self._speech_origin + b]
                            for a, b in self._speech_detector.feed(b"", final=True)
                        ]
                    )
                self._speech_detector = SpeechDetector(self.audio_format.sample_rate)
                self._speech_origin = row["start"]
            intervals = self._speech_detector.feed(pcm)
            self._speech_intervals.extend(
                [
                    [self._speech_origin + a, self._speech_origin + b]
                    for a, b in intervals
                ]
            )
            self._speech_end = row["end"]
            self._media_rows.append(row)
        if not self.playback.queue and self._speech_origin is not None:
            self._speech_intervals.extend(
                [
                    [self._speech_origin + a, self._speech_origin + b]
                    for a, b in self._speech_detector.feed(b"", final=True)
                ]
            )
            self._speech_origin = None
            self._speech_end = None

    async def _media_event(self, handler, event):
        at = time.monotonic()
        self._drain_playback(at)
        live = self._stream_result
        live.agent_audio_chunks = self.playback.pending_chunks(at)
        before = list(live.agent_audio_chunks)
        # A bridge's playback counters describe actual presentation, not receipt.
        if hasattr(self, "_played"):
            for rid, count in self.playback.played.items():
                self._played[rid] = count
        previous_tools = len(live.tool_calls)
        answer = handler(live, event)
        if inspect.isawaitable(answer):
            await answer
        for call in live.tool_calls[previous_tools:]:
            live.media_tool_calls.append(dict(call_id=call.id, started_mono=at))
        after = live.agent_audio_chunks
        from collections import Counter

        previous = Counter()
        current = Counter()
        for data, rid in before:
            previous[rid] += len(data)
        for data, rid in after:
            current[rid] += len(data)
        removed = {rid for rid, count in previous.items() if current[rid] < count}
        if removed:
            self.playback.cancel(removed, at)
        # Existing protocol handlers append converted audio after pending chunks.
        skip = Counter(previous)
        for data, rid in after:
            n = min(skip[rid], len(data))
            skip[rid] -= n
            if n < len(data):
                self.playback.append(
                    data[n:],
                    rid,
                    event.get("received_monotonic")
                    if isinstance(event, dict)
                    else getattr(event, "received_monotonic", None),
                )
        live.agent_audio_chunks = self.playback.pending_chunks()

    def _start_media_receiver(self, receive, handler):
        """Keep receiving and scheduling playback while task ticks are idle."""
        if self._media_task is not None:
            return

        async def pump():
            while True:
                for event in await receive():
                    await self._media_event(handler, event)
                await asyncio.sleep(0)

        self._media_task = asyncio.create_task(pump())

    @abstractmethod
    async def _execute_tick(
        self,
        user_audio: bytes,
        tick_number: int,
        result: TickResult,
        tick_start: float,
    ) -> None:
        """Provider-specific tick execution. Mutate result in place.

        Args:
            user_audio: User audio bytes (in external format).
            tick_number: Current tick number.
            result: TickResult to populate with events, audio, etc.
            tick_start: asyncio loop time when the tick started. Use this
                to compute remaining time for receive_events_for_duration().

        Responsibilities:
        - Convert audio format if needed
        - Send audio to provider API
        - Receive events for the remaining tick duration
        - Process events (append to result.agent_audio_chunks,
          result.tool_calls, result.vad_events, result.events)
        - Track utterance transcripts via self._utterance_transcripts
        - Set result.was_truncated and result.skip_item_id on barge-in
        """
        raise NotImplementedError

    @property
    def has_pending_output(self) -> bool:
        """Expose buffered media and tool replies without altering playback."""
        return bool(self.playback.pending_chunks() or self._pending_tool_results)

    @abstractmethod
    async def _flush_pending_tool_results(self) -> None:
        """Send all pending tool results to the provider.

        Called at the start of each tick. Provider-specific because each
        API has different batching semantics (e.g., Gemini batches all
        results in one call, others send individually).

        After sending, clear self._pending_tool_results.
        """
        raise NotImplementedError

    # -----------------------------------------------------------------------
    # Shared helpers
    # -----------------------------------------------------------------------

    def _record_input(self, data, fraction):
        context = self._input_context
        if context is None:
            return
        now = time.monotonic()
        duration = context["duration"] * fraction
        context.setdefault("started", now)
        offset = context.get("sent_duration", 0.0)
        start = max(now, context["started"] + offset)
        context["sent_duration"] = offset + duration
        speech = context["speech"]
        left, right = max(offset, speech[0]), min(offset + duration, speech[1])
        row = dict(
            role="user",
            start=start,
            end=start + duration,
            speech_intervals=[[start + left - offset, start + right - offset]]
            if right > left
            else [],
            utterance_ids=context["utterance_ids"],
            audio=base64.b64encode(
                context["audio"][
                    round(offset * self.audio_format.bytes_per_second) : round(
                        (offset + duration) * self.audio_format.bytes_per_second
                    )
                ]
            ).decode(),
            format=self.audio_format.model_dump(mode="json"),
        )
        if "event_kind" in context:
            row["user_event_kind"] = context["event_kind"]
            row["user_event_version"] = USER_EVENT_VERSION
            row["independent_user_events"] = []
            for event in context.get("independent_events", []):
                a, b = (
                    max(offset, event["range"][0]),
                    min(offset + duration, event["range"][1]),
                )
                if b > a:
                    row["independent_user_events"].append(
                        dict(
                            id=event["id"],
                            kind=event["kind"],
                            intervals=[[start + a - offset, start + b - offset]],
                        )
                    )
        self._media_rows.append(row)

    async def _send_audio_chunked(
        self,
        audio: bytes,
        send_fn: Callable[[bytes], Awaitable[None]],
        chunk_size: int,
    ) -> None:
        """Send audio either instantly or in VoIP-style chunks.

        Args:
            audio: Audio bytes to send (in provider's format).
            send_fn: Async callable that sends a chunk to the provider.
            chunk_size: Bytes per chunk when using VoIP-style sending.
        """
        if len(audio) == 0:
            return
        if self.send_audio_instant:
            self._record_input(audio, 1.0)
            await send_fn(audio)
        else:
            offset = 0
            while offset < len(audio):
                chunk = audio[offset : offset + chunk_size]
                self._record_input(chunk, len(chunk) / len(audio))
                await send_fn(chunk)
                offset += len(chunk)
                await asyncio.sleep(self._voip_interval_ms / 1000)


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------

# Providers where the model is determined by the endpoint, not a parameter
_PROVIDERS_WITH_ENDPOINT_DETERMINED_MODEL: tuple[str, ...] = ()


def create_adapter(
    provider: str,
    tick_duration_ms: int,
    send_audio_instant: bool = DEFAULT_SEND_AUDIO_INSTANT,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    audio_format: Optional[AudioFormat] = None,
    cascaded_config: Any = None,
    live_config: Optional["LiveConfig"] = None,
    trace_path: Optional[Path] = None,
    voice: Optional[str] = None,
    transcription_language: Optional[str] = None,
    language: Optional[str] = None,
    avartha_config: Optional["AvarthaPipecatConfig"] = None,
    qwen_agent_config: Optional["QwenAgentConfig"] = None,
    observer_hotwords: Any = None,
) -> Tuple[DiscreteTimeAdapter, str]:
    """Create a discrete-time adapter for the given provider.

    Validates parameter/provider compatibility, resolves the model default,
    constructs the appropriate adapter subclass, and returns both the adapter
    and the resolved model name.

    Args:
        provider: Provider identifier (openai, gemini, xai, nova, qwen,
            avartha_pipecat, phonic, livekit).
        tick_duration_ms: Duration of each tick in milliseconds.
        send_audio_instant: If True, send audio in one call per tick.
        model: Model identifier. If None, uses the provider's default.
        audio_format: Audio format for external communication. Defaults to
            telephony (8kHz μ-law).
        cascaded_config: Configuration for cascaded providers (livekit).
        live_config: Frontend and delegated backend configuration for OpenAI Live.
        trace_path: Optional private protocol trace for OpenAI Live.
        language: Resolved run language for supported provider hints. OpenAI
            input transcription retains its explicit transcription_language setting.
        avartha_config: Connection settings for a hosted Avartha agent.

    Returns:
        Tuple of (adapter, resolved_model).

    Raises:
        ValueError: If the provider is unknown.
    """
    if voice is not None and provider != "openai":
        raise ValueError(
            "voice override currently requires openai; Live uses live_config.voice"
        )
    # --- Resolve reasoning_effort default ---
    if reasoning_effort is None:
        reasoning_effort = DEFAULT_AUDIO_NATIVE_REASONING_EFFORT.get(provider)

    # --- Resolve model default ---
    if model is None:
        if provider == "livekit":
            from tau2.data_model.cascaded import CascadedConfig

            config = cascaded_config or CascadedConfig()
            model = config.llm.model
        else:
            model = DEFAULT_AUDIO_NATIVE_MODELS[provider]
        logger.debug(
            f"No model provided, using default model for provider {provider}: {model}"
        )
    elif provider in _PROVIDERS_WITH_ENDPOINT_DETERMINED_MODEL:
        logger.warning(
            f"model='{model}' was provided but the '{provider}' provider's model "
            f"is determined by its endpoint — the provided model will be ignored."
        )

    # --- Construct adapter ---
    adapter: DiscreteTimeAdapter
    if provider == "launa":
        from tau2.voice.audio_native.launa.discrete_time_adapter import (
            DiscreteTimeLaunaAdapter,
        )

        adapter = DiscreteTimeLaunaAdapter(
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
            audio_format=audio_format,
        )
    elif provider == "openai":
        from tau2.voice.audio_native.openai.discrete_time_adapter import (
            DiscreteTimeOpenAIAdapter,
        )

        adapter = DiscreteTimeOpenAIAdapter(
            voice=voice,
            transcription_language=transcription_language,
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
            audio_format=audio_format,
        )
    elif provider == "openai_live":
        from tau2.voice.audio_native.openai.live_adapter import (
            DiscreteTimeOpenAILiveAdapter,
        )

        if live_config is None:
            raise ValueError("openai_live requires live_config with a backend model")
        adapter = DiscreteTimeOpenAILiveAdapter(
            tick_duration_ms=tick_duration_ms,
            model=model,
            config=live_config,
            reasoning_effort=reasoning_effort,
            send_audio_instant=send_audio_instant,
            audio_format=audio_format,
            trace_path=trace_path,
        )
    elif provider == "gemini":
        from tau2.voice.audio_native.gemini.discrete_time_adapter import (
            DiscreteTimeGeminiAdapter,
        )

        adapter = DiscreteTimeGeminiAdapter(
            language=language,
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "xai":
        from tau2.voice.audio_native.xai.discrete_time_adapter import (
            DiscreteTimeXAIAdapter,
        )

        adapter = DiscreteTimeXAIAdapter(
            language=language,
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "nova":
        from tau2.voice.audio_native.nova.discrete_time_adapter import (
            DiscreteTimeNovaAdapter,
        )

        adapter = DiscreteTimeNovaAdapter(
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "qwen":
        from tau2.voice.audio_native.qwen.discrete_time_adapter import (
            DiscreteTimeQwenAdapter,
        )

        adapter = DiscreteTimeQwenAdapter(
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "avartha_pipecat":
        from tau2.voice.audio_native.avartha_pipecat.discrete_time_adapter import (
            DiscreteTimeAvarthaPipecatAdapter,
        )

        adapter = DiscreteTimeAvarthaPipecatAdapter(
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
            audio_format=audio_format,
            avartha_config=avartha_config,
        )
    elif provider == "qwen_agent":
        from tau2.voice.audio_native.qwen_agent.discrete_time_adapter import (
            DiscreteTimeQwenAgentAdapter,
        )

        if qwen_agent_config is None:
            raise ValueError("qwen_agent requires qwen_agent_config")
        adapter = DiscreteTimeQwenAgentAdapter(
            tick_duration_ms=tick_duration_ms,
            config=qwen_agent_config,
            observer_hotwords=observer_hotwords,
            model=model,
            reasoning_effort=reasoning_effort,
            audio_format=audio_format,
            language=language,
            trace_path=trace_path,
        )
    elif provider == "phonic":
        from tau2.voice.audio_native.phonic.discrete_time_adapter import (
            DiscreteTimePhonicAdapter,
        )

        adapter = DiscreteTimePhonicAdapter(
            tick_duration_ms=tick_duration_ms,
            send_audio_instant=send_audio_instant,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    elif provider == "livekit":
        from tau2.data_model.cascaded import CascadedConfig
        from tau2.voice.audio_native.livekit.discrete_time_adapter import (
            LiveKitCascadedAdapter,
        )

        config = cascaded_config or CascadedConfig()
        adapter = LiveKitCascadedAdapter(
            tick_duration_ms=tick_duration_ms,
            cascaded_config=config,
            send_audio_instant=send_audio_instant,
            audio_format=audio_format,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")

    return adapter, model

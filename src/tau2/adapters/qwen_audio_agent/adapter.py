"""Tick adapter for a complete Gateway, orchestrator and native Qwen Code."""

import audioop
import base64
from collections import Counter

from tau2.adapters.qwen_audio_agent.provider import QwenAgentProvider
from tau2.adapters.qwen_audio_agent.transcription.base import PlayedAudioTranscriber
from tau2.config import (
    DEFAULT_AUDIO_NATIVE_DISCONNECT_TIMEOUT,
    DEFAULT_QWEN_ASR_STREAMING_MODEL,
    DEFAULT_TELEPHONY_RATE,
)
from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT, AudioEncoding, AudioFormat
from tau2.data_model.message import ToolCall
from tau2.data_model.transcript import LiveTranscriptTick
from tau2.voice.audio_native.adapter import DiscreteTimeAdapter
from tau2.voice.audio_native.async_loop import BackgroundAsyncLoop
from tau2.voice.audio_native.audio_converter import StreamingTelephonyConverter
from tau2.voice.audio_native.tick_result import PlaybackCancellation


class DiscreteTimeQwenAgentAdapter(DiscreteTimeAdapter):
    """Translate media and MCP requests; leave all agent decisions to Qwen."""

    def __init__(
        self,
        tick_duration_ms,
        config,
        model=None,
        reasoning_effort=None,
        audio_format=None,
        language=None,
        trace_path=None,
        provider=None,
        transcriber=None,
        observer_hotwords=None,
    ):
        if reasoning_effort is not None:
            raise ValueError("Configure reasoning on the Qwen system")
        if audio_format is not None and audio_format != TELEPHONY_AUDIO_FORMAT:
            raise ValueError("qwen_agent expects 8 kHz mu-law audio")
        if language not in (None, "en", "en-US"):
            raise ValueError("The first Qwen Agent integration supports English only")
        super().__init__(tick_duration_ms, audio_format, send_audio_instant=False)
        self.playback_audio_format = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE,
            sample_rate=config.playback_sample_rate,
        )
        self.playback_bytes_per_tick = int(
            self.playback_audio_format.bytes_per_second * tick_duration_ms / 1000
        )
        self._native_playback = config.asr_model == DEFAULT_QWEN_ASR_STREAMING_MODEL
        if self._native_playback:
            self.playback = self.playback.__class__(self.playback_audio_format)
        self.realtime_pacing = True
        self.config, self.model = config, model
        self.provider = provider or QwenAgentProvider(config, trace_path)
        if transcriber is not None:
            self.transcriber = transcriber
        elif config.asr_provider == "qwen":
            if config.asr_model == DEFAULT_QWEN_ASR_STREAMING_MODEL:
                from tau2.adapters.qwen_audio_agent.transcription.qwen_streaming import (
                    QwenStreamingPlayedAudioTranscriber,
                )

                self.transcriber = QwenStreamingPlayedAudioTranscriber(
                    config, hotwords=observer_hotwords
                )
            else:
                from tau2.adapters.qwen_audio_agent.transcription.qwen import (
                    QwenPlayedAudioTranscriber,
                )

                self.transcriber = QwenPlayedAudioTranscriber(
                    config, hotwords=observer_hotwords
                )
        elif config.asr_provider == "mimo":
            from tau2.adapters.qwen_audio_agent.transcription.mimo import (
                MiMoPlayedAudioTranscriber,
            )

            self.transcriber = MiMoPlayedAudioTranscriber(config)
        else:
            self.transcriber = PlayedAudioTranscriber(
                config.asr_model, config.asr_api_key_env, "en"
            )
        self._bg_loop = BackgroundAsyncLoop()
        self._connected = False
        self._converters = {}
        self._received = Counter()
        self._played = Counter()
        self._done = set()
        self._terminal = set()
        self._playback_started = set()
        self._cancelled = set()
        self._seen_calls = set()
        self._allowed_tools = set()
        self._ticks = 0
        self._frontend_state = None
        self.final_live_transcript = None
        self.final_live_transcript_error = None
        self._generated_response_buffers = {}

    @property
    def is_connected(self):
        """The live system and observer must both be connected."""
        return self._connected and self.provider.is_connected

    def enable_diagnostics(self):
        """Enable passive bridge recording before connection."""
        self.provider.diagnostics_enabled = True

    def get_diagnostic_evidence(self):
        """Expose observed backend inputs, without executing native operations."""
        provider = self.provider
        return {
            "status": "complete" if provider.backend_observer_ready else "unavailable",
            "reason": None
            if provider.backend_observer_ready
            else "bridge_backend_observer_not_available",
            "backend_inputs": list(provider.backend_inputs.values()),
            "state_observations": list(getattr(provider, "state_observations", [])),
        }

    def get_run_metadata(self):
        """Keep bridge-specific provenance and missing usage knowledge local."""
        return {
            "cost_available": False,
            "system_metadata": {
                **self.provider.metadata,
                "observer_asr": self.config.asr_model,
                "transcript_origin": "recorded_audio",
                "tick_duration_ms": self.tick_duration_ms,
                "observer_finalization_latency": self.transcriber.summary(),
            },
        }

    def get_final_live_transcript(self):
        """Read the final observer transcript after disconnect."""
        return self.final_live_transcript, self.final_live_transcript_error

    def get_async_evidence_source(self):
        """Return the provider's implementation of the public evidence contract."""
        from tau2.adapters.qwen_audio_agent.evidence import (
            QwenAsyncEvidenceSource,
        )

        if not hasattr(self, "_async_evidence_source"):
            self._async_evidence_source = QwenAsyncEvidenceSource(self)
        return self._async_evidence_source

    def drain_backend_observations(self):
        """Return Qwen backend events for persistent-task projection."""
        from tau2.adapters.qwen_audio_agent.events import QwenEventAdapter

        adapter = QwenEventAdapter()
        return [
            adapter.backend_observation(payload)
            for payload in self.provider.drain_backend_events()
        ]

    def connect(self, system_prompt, tools, vad_config=None, modality="audio"):
        """Boot a fresh system with only the public business environment."""
        if modality != "audio":
            raise ValueError("qwen_agent requires full audio input and output")
        if self._connected:
            self.disconnect()
        self.clear_buffers()
        self._frontend_state = None
        self._next_tick_start = None
        self._cumulative_user_audio_ms = 0
        self._ticks = 0
        self._converters.clear()
        self._received.clear()
        self._played.clear()
        self._done.clear()
        self._terminal.clear()
        self._playback_started.clear()
        self._cancelled.clear()
        self._seen_calls.clear()
        self._allowed_tools = {t.name for t in tools}
        self.final_live_transcript = None
        self.final_live_transcript_error = None
        self._generated_response_buffers.clear()
        self._bg_loop.start()
        try:
            self._bg_loop.run_coroutine(
                self._connect(system_prompt, tools),
                timeout=self.config.boot_timeout_seconds + 30,
            )
            self._connected = True
        except BaseException:
            self.disconnect()
            raise

    async def _connect(self, policy, tools):
        # Validate the observer before starting billable agent work.
        self.transcriber.validate_config()
        await self.provider.connect(policy, tools)
        await self.transcriber.connect()
        self._input_converter = StreamingTelephonyConverter(
            input_sample_rate=self.provider.input_sample_rate,
        )

    def run_tick(self, user_audio, tick_number=None):
        """Run media continuously, then acknowledge only the capped playback."""
        if not self.is_connected:
            raise RuntimeError("Qwen Agent is not connected")
        self._ticks += 1
        tick_number = self._ticks if tick_number is None else tick_number
        result = self._bg_loop.run_coroutine(
            self._async_run_tick(user_audio, tick_number),
            timeout=self.tick_duration_ms / 1000 + 10,
        )
        self._bg_loop.run_coroutine(self._after_playback(result), timeout=10)
        return result

    def set_observer_stage(self, stage: int) -> None:
        """Activate the next staged vocabulary on the observer event loop."""
        activate = getattr(self.transcriber, "activate_stage", None)
        if activate is None:
            return
        self._bg_loop.run_coroutine(activate(stage), timeout=10)

    async def _flush_pending_tool_results(self):
        for call_id, result, _request_response, is_error in self._pending_tool_results:
            await self.provider.send(
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "content": result,
                    "is_error": is_error,
                }
            )
        self._pending_tool_results.clear()

    def recheck_heard_audio(self, evidence, fragments):
        """Called only by the background caller generator, not the media tick."""
        recheck = getattr(self.transcriber, "recheck", None)
        if recheck is None or not self.is_connected:
            return {"status": "unavailable"}
        try:
            return self._bg_loop.run_coroutine(
                recheck(evidence, fragments),
                timeout=self.config.asr_recheck_timeout_seconds + 1,
            )
        except Exception:
            return {"status": "error"}

    async def _execute_tick(self, user_audio, tick_number, result, tick_start):
        result.playback_cancellations = []
        pcm = self._input_converter.convert_input(user_audio)

        async def send_audio(chunk):
            await self.provider.send(
                {
                    "type": "input_audio",
                    "audio": base64.b64encode(chunk).decode(),
                }
            )

        async def receive():
            event = await self.provider.next_event()
            return [event]

        self._start_media_receiver(receive, self._process_event)
        await self._send_audio_chunked(
            pcm,
            send_audio,
            int(self.provider.input_sample_rate * 2 * self._voip_interval_ms / 1000),
        )
        if getattr(self.provider, "failure", None):
            raise self.provider.failure

    @property
    def has_pending_output(self) -> bool:
        """Include audio responses whose playback has not reached its end receipt."""
        return super().has_pending_output or bool(set(self._received) - self._terminal)

    @property
    def backend_request_boundary(self):
        """A native frontend idle signal plus completed physical playback."""
        return self._frontend_state == "idle" and not self.has_pending_output

    def _process_event(self, result, event):
        """Map business requests and media; never expose internal task text."""
        kind = event.get("type")
        if kind == "gateway_event":
            self._collect_generated_text(result, event.get("event") or {})
        if (
            kind == "gateway_event"
            and event.get("event", {}).get("type") == "voice.state"
        ):
            self._frontend_state = event["event"].get("state")
        result.events.append({k: v for k, v in event.items() if k != "audio"})
        if kind == "audio":
            response_id = event["response_id"]
            if response_id in self._cancelled:
                return
            rate = int(event["sample_rate"])
            pcm = base64.b64decode(event["audio"], validate=True)
            if self._native_playback:
                if rate != self.playback_audio_format.sample_rate or len(pcm) % 2:
                    raise ValueError(
                        "Qwen native PCM rate or sample alignment mismatch"
                    )
                data = pcm
            else:
                converter = self._converters.setdefault(
                    rate, StreamingTelephonyConverter(output_sample_rate=rate)
                )
                data = converter.convert_output(pcm)
            self._received[response_id] += len(data)
            result.agent_audio_chunks.append((data, response_id))
        elif kind == "audio_done":
            self._done.add(event["response_id"])
        elif kind == "clear":
            ids = (
                {event["response_id"]}
                if event.get("response_id")
                else set(self._received)
            )
            completed = {
                response_id
                for response_id in ids
                if response_id in self._terminal
                or (
                    response_id in self._done
                    and self._played[response_id] >= self._received[response_id]
                )
            }
            ids -= completed
            if result.playback_cancellations is None:
                result.playback_cancellations = []
            for response_id in sorted(completed):
                result.playback_cancellations.append(
                    PlaybackCancellation(
                        response_id=response_id,
                        status="already_completed",
                        received_bytes=self._received[response_id],
                        played_bytes=self._played[response_id],
                    )
                )
            if not ids and not completed:
                result.playback_cancellations.append(
                    PlaybackCancellation(
                        response_id=None,
                        status="no_pending_output",
                    )
                )
            interrupted = False
            for response_id in sorted(ids):
                discarded = sum(
                    len(data)
                    for data, item in result.agent_audio_chunks
                    if item == response_id
                )
                active = self._received[response_id] > self._played[response_id] or (
                    self._received[response_id] > 0 and response_id not in self._done
                )
                interrupted |= active
                result.playback_cancellations.append(
                    PlaybackCancellation(
                        response_id=response_id,
                        status="cancelled",
                        interrupted=active,
                        received_bytes=self._received[response_id],
                        played_bytes=self._played[response_id],
                        discarded_bytes=discarded,
                    )
                )
            self._cancelled.update(ids)
            result.truncated_audio_bytes += sum(
                len(data) for data, item in result.agent_audio_chunks if item in ids
            )
            result.agent_audio_chunks = [
                (data, item)
                for data, item in result.agent_audio_chunks
                if item not in ids
            ]
            result.was_truncated |= interrupted
            # Filtering above is response-specific. The shared buffer must not
            # discard the remainder of an unrelated new response in this tick.
            result.audio_truncation_applied = True
            if interrupted:
                for converter in self._converters.values():
                    converter.reset()
            # Do not set skip_item_id: it would also discard a new reply this tick.
            if interrupted:
                result.vad_events.append("interrupted")
        elif kind == "tool_call":
            call_id = event["call_id"]
            if call_id in self._seen_calls:
                raise RuntimeError("Duplicate business tool request; refusing replay")
            if event["name"] not in self._allowed_tools:
                raise RuntimeError("Bridge requested an unregistered business tool")
            self._seen_calls.add(call_id)
            result.tool_calls.append(
                ToolCall(
                    id=call_id,
                    name=event["name"],
                    arguments=event["arguments"],
                    requestor="assistant",
                )
            )

    def _collect_generated_text(self, result, event):
        """Collect Qwen response text without treating it as played-audio ASR."""
        event_type = event.get("type") or ""
        if event_type not in {
            "transcript.delta",
            "transcript.final",
            "response.audio_transcript.delta",
            "response.audio_transcript.done",
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
        }:
            return
        response_id = event.get("responseId") or event.get("response_id")
        text = (
            event.get("content") or event.get("transcript") or event.get("delta") or ""
        )
        from tau2.utils.transcript.generated import sanitize_generated_text

        text = sanitize_generated_text(str(text))
        if not response_id or not text:
            return
        buffer = self._generated_response_buffers.setdefault(
            response_id, {"text": "", "final": False}
        )
        is_final = event_type.endswith("final") or event_type.endswith("done")
        if is_final:
            buffer["text"] = str(text)
            buffer["final"] = True
        elif not buffer["final"]:
            buffer["text"] += str(text)
        result.generated_text.append(
            {
                "response_id": response_id,
                "turn_id": event.get("turnId") or event.get("turn_id"),
                "text": text,
                "source": "qwen_response_transcript",
                "received_monotonic": event.get("received_monotonic"),
                "final": is_final,
            }
        )

    async def _playback_progress(self):
        for response_id, count in list(self.playback.played.items()):
            self._played[response_id] = count
            if count and response_id not in self._playback_started:
                self._playback_started.add(response_id)
                await self._receipt("playback.started", response_id)
        for response_id in self._cancelled - self._terminal:
            await self._receipt("playback.cancelled", response_id)
            self._terminal.add(response_id)
        for response_id in self._done - self._terminal:
            if self._played[response_id] >= self._received[response_id]:
                await self._receipt("playback.ended", response_id)
                self._terminal.add(response_id)

    async def _after_playback(self, result):
        await self._playback_progress()
        played = result.get_played_agent_audio()
        if hasattr(self.transcriber, "send_native"):
            if self._native_playback:
                await self.transcriber.send_native(
                    played, self.playback_audio_format.sample_rate
                )
            else:
                await self.transcriber.send_native(
                    audioop.ulaw2lin(played, 2), DEFAULT_TELEPHONY_RATE
                )
        else:
            await self.transcriber.send(audioop.ulaw2lin(played, 2))
        if result.was_truncated and result.contains_speech:
            queue_partial = getattr(self.transcriber, "queue_pending", None)
            if queue_partial is not None:
                queue_partial()
        fragments = self.transcriber.drain()
        result.proportional_transcript = " ".join(f.text for f in fragments)
        result.live_transcript = LiveTranscriptTick(
            session_id=self.provider.session_id,
            origin="recorded_audio",
            alignment="played_audio",
            fragments=fragments,
            speech_intervals_ms=[
                (
                    (a - result.playback_start_monotonic) * 1000,
                    (b - result.playback_start_monotonic) * 1000,
                )
                for a, b in result.speech_intervals
            ],
        )

    async def _receipt(self, kind, response_id):
        await self.provider.send(
            {"type": "playback", "event": kind, "response_id": response_id}
        )

    async def _disconnect(self):
        try:
            if self.provider.is_connected:
                await self._flush_pending_tool_results()
        finally:
            try:
                await self._flush_observer()
            finally:
                try:
                    await self.transcriber.close()
                finally:
                    await self.provider.disconnect()

    async def _flush_observer(self):
        """Finalize played-audio ASR before cancelling its worker."""
        flush = getattr(self.transcriber, "flush_pending", None)
        if flush is None:
            self.final_live_transcript_error = "observer_flush_not_supported"
            return
        try:
            fragments = await flush()
            if not fragments:
                self.final_live_transcript = None
                return
            intervals = [
                (fragment.start_ms or 0, fragment.end_ms or fragment.start_ms or 0)
                for fragment in fragments
                if fragment.start_ms is not None and fragment.end_ms is not None
            ]
            from tau2.data_model.transcript import LiveTranscriptTick

            self.final_live_transcript = LiveTranscriptTick(
                session_id=self.provider.session_id,
                origin="recorded_audio",
                alignment="played_audio",
                fragments=fragments,
                speech_intervals_ms=intervals,
            )
        except Exception as error:
            self.final_live_transcript = None
            self.final_live_transcript_error = type(error).__name__

    def disconnect(self):
        """Reject later writes and always tear down the owned system."""
        try:
            if self._bg_loop.is_running:
                self._bg_loop.run_coroutine(
                    self._disconnect(),
                    timeout=self.config.boot_timeout_seconds
                    + DEFAULT_AUDIO_NATIVE_DISCONNECT_TIMEOUT,
                )
        finally:
            self._bg_loop.stop()
            self._connected = False
            self.clear_buffers()

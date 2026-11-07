"""Tick adapter for a complete Gateway, orchestrator and native Qwen Code."""

import audioop
import base64
from collections import Counter

from tau2.config import DEFAULT_AUDIO_NATIVE_DISCONNECT_TIMEOUT
from tau2.data_model.audio import TELEPHONY_AUDIO_FORMAT
from tau2.data_model.message import ToolCall
from tau2.data_model.transcript import LiveTranscriptTick
from tau2.voice.audio_native.adapter import DiscreteTimeAdapter
from tau2.voice.audio_native.async_loop import BackgroundAsyncLoop
from tau2.voice.audio_native.audio_converter import StreamingTelephonyConverter
from tau2.voice.audio_native.qwen_agent.provider import QwenAgentProvider
from tau2.voice.audio_native.qwen_agent.transcription import PlayedAudioTranscriber


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
        self.realtime_pacing = True
        self.config, self.model = config, model
        self.provider = provider or QwenAgentProvider(config, trace_path)
        if transcriber is not None:
            self.transcriber = transcriber
        elif config.asr_provider == "qwen":
            from tau2.voice.audio_native.qwen_agent.qwen_transcription import (
                QwenPlayedAudioTranscriber,
            )

            self.transcriber = QwenPlayedAudioTranscriber(
                config, hotwords=observer_hotwords
            )
        elif config.asr_provider == "mimo":
            from tau2.voice.audio_native.qwen_agent.mimo_transcription import (
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
        self._cancelled = set()
        self._seen_calls = set()
        self._allowed_tools = set()
        self._ticks = 0

    @property
    def is_connected(self):
        """The live system and observer must both be connected."""
        return self._connected and self.provider.is_connected

    def connect(self, system_prompt, tools, vad_config=None, modality="audio"):
        """Boot a fresh system with only the public business environment."""
        if modality != "audio":
            raise ValueError("qwen_agent requires full audio input and output")
        if self._connected:
            self.disconnect()
        self.clear_buffers()
        self._next_tick_start = None
        self._cumulative_user_audio_ms = 0
        self._ticks = 0
        self._converters.clear()
        self._received.clear()
        self._played.clear()
        self._done.clear()
        self._terminal.clear()
        self._cancelled.clear()
        self._seen_calls.clear()
        self._allowed_tools = {t.name for t in tools}
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
        pcm = self._input_converter.convert_input(user_audio)

        async def send_audio(chunk):
            await self.provider.send(
                {
                    "type": "input_audio",
                    "audio": base64.b64encode(chunk).decode(),
                }
            )

        await self._send_audio_chunked(
            pcm,
            send_audio,
            int(self.provider.input_sample_rate * 2 * self._voip_interval_ms / 1000),
        )
        for event in self.provider.drain():
            self._process_event(result, event)
        buffered = sum(len(data) for data, _ in result.agent_audio_chunks)
        if (
            buffered
            > self.config.max_buffer_seconds * self.audio_format.bytes_per_second
        ):
            raise RuntimeError(
                "Qwen playback backlog exceeded the configured limit "
                f"({buffered / self.audio_format.bytes_per_second:.1f}s queued, "
                f"{self.config.max_buffer_seconds:.1f}s allowed)"
            )

    @property
    def has_pending_output(self) -> bool:
        """Include audio responses whose playback has not reached its end receipt."""
        return super().has_pending_output or bool(set(self._received) - self._terminal)

    def _process_event(self, result, event):
        """Map business requests and media; never expose internal task text."""
        kind = event.get("type")
        result.events.append({k: v for k, v in event.items() if k != "audio"})
        if kind == "audio":
            response_id = event["response_id"]
            if response_id in self._cancelled:
                return
            rate = int(event["sample_rate"])
            converter = self._converters.setdefault(
                rate, StreamingTelephonyConverter(output_sample_rate=rate)
            )
            data = converter.convert_output(
                base64.b64decode(event["audio"], validate=True)
            )
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
            ids -= self._terminal
            self._cancelled.update(ids)
            result.truncated_audio_bytes += sum(
                len(data) for data, item in result.agent_audio_chunks if item in ids
            )
            result.agent_audio_chunks = [
                (data, item)
                for data, item in result.agent_audio_chunks
                if item not in ids
            ]
            result.was_truncated = True
            for converter in self._converters.values():
                converter.reset()
            # Do not set skip_item_id: it would also discard a new reply this tick.
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

    async def _after_playback(self, result):
        for data, response_id in result.agent_audio_chunks:
            if data and not self._played[response_id]:
                await self._receipt("playback.started", response_id)
            self._played[response_id] += len(data)
        for response_id in self._cancelled - self._terminal:
            await self._receipt("playback.cancelled", response_id)
            self._terminal.add(response_id)
        for response_id in self._done - self._terminal:
            if self._played[response_id] >= self._received[response_id]:
                await self._receipt("playback.ended", response_id)
                self._terminal.add(response_id)
        played = result.get_played_agent_audio()
        await self.transcriber.send(audioop.ulaw2lin(played, 2))
        fragments = self.transcriber.drain()
        result.proportional_transcript = " ".join(f.text for f in fragments)
        result.contains_speech = bool(
            result.agent_audio_data
            and audioop.rms(audioop.ulaw2lin(result.agent_audio_data, 2), 2)
        )
        result.live_transcript = LiveTranscriptTick(
            session_id=self.provider.session_id,
            origin="recorded_audio",
            alignment="played_audio",
            fragments=fragments,
            speech_intervals_ms=(
                [
                    (
                        0,
                        len(result.agent_audio_data)
                        / self.audio_format.bytes_per_second
                        * 1000,
                    )
                ]
                if result.contains_speech
                else []
            ),
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
                await self.transcriber.close()
            finally:
                await self.provider.disconnect()

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

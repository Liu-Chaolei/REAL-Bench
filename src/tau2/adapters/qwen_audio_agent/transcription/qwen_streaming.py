"""Sentence-final streaming ASR of audio actually played to the caller."""

import asyncio
import audioop
import hashlib
import json
import time
import uuid
from contextvars import ContextVar

import websockets
from pydantic import BaseModel, ConfigDict, Field
from websockets.exceptions import ConnectionClosed, InvalidStatus

from tau2.adapters.qwen_audio_agent.transcription.qwen import (
    QwenPlayedAudioTranscriber,
)
from tau2.config import (
    DEFAULT_QWEN_ASR_MODEL,
    DEFAULT_TELEPHONY_RATE,
)
from tau2.data_model.transcript import LiveTranscriptFragment, LiveTranscriptSnapshot
from tau2.voice.qwen_asr import qwen_streaming_asr_url, transcribe_qwen


class _ASRTaskFailure(RuntimeError):
    """Classify service failures without retaining private provider messages."""

    def __init__(self, code):
        super().__init__("Qwen ASR task failed")
        normalized = (
            code.lower().replace("_", "").replace(".", "")
            if isinstance(code, str)
            else ""
        )
        self.retryable = normalized in {
            "internalerror",
            "internalservererror",
            "servererror",
            "serviceunavailable",
            "requesttimeout",
            "throttling",
            "throttlingratelimit",
            "toomanyrequests",
        }


def _retryable(error):
    if isinstance(error, _ASRTaskFailure):
        return error.retryable
    if isinstance(error, InvalidStatus):
        return error.response.status_code in {408, 429, 500, 502, 503, 504}
    return isinstance(error, (TimeoutError, OSError, ConnectionClosed))


class _SpeechSession(BaseModel):
    """One speech episode; private transport objects never enter the result."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    start_ms: float
    sent_end_ms: float
    sample_rate: int
    hotword_stage: int | None = None
    vocabulary: dict[str, int] = Field(default_factory=dict)
    vocabulary_sha256: str
    end_ms: float | None = None
    status: str = "queued"
    next_sentence: int = 1
    sentences: dict[int, dict] = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    attempts: list[dict] = Field(default_factory=list)
    replay_available: bool = True
    input_consumed: bool = False
    replay_pcm_bytes: int = Field(default=0, exclude=True)
    replay_audio: list[tuple[bytes, float, float]] = Field(
        default_factory=list, exclude=True
    )
    attempt_next_sentence: int = Field(default=1, exclude=True)
    attempt_sentences: dict[int, dict] = Field(default_factory=dict, exclude=True)
    queue: asyncio.Queue = Field(
        default_factory=lambda: asyncio.Queue(maxsize=16), exclude=True
    )


class QwenStreamingPlayedAudioTranscriber(QwenPlayedAudioTranscriber):
    """Append bounded audio batches, exposing only complete confirmed sentences.

    Media ticks only enqueue audio; connection startup, ASR, bounded reconnects
    and finish-task all run in the background. Silence or an actual playback cut
    ends the episode; new speech opens a fresh task.
    """

    def __init__(self, config, language="en", client=None, hotwords=None):
        super().__init__(config, language, client, hotwords)
        self.sessions = []
        self.events = []
        self.appends = []
        self.versions = []
        self._active = None
        self._confirmed = ""
        self._version = 0
        self.played_sample_rate = config.playback_sample_rate
        self._hotword_stage = 0
        self.hotword_activations = []
        self._review_stage = ContextVar("observer_review_stage", default=None)

    def validate_config(self):
        """Resolve private observer credentials before starting the Agent."""
        super().validate_config()
        self._stream_url = qwen_streaming_asr_url(self._url)

    async def connect(self):
        """Initialize the observer; defer network startup until actual speech."""
        self.sessions.clear()
        self.events.clear()
        self.appends.clear()
        self.versions = [{"version": 0, "text": "", "released_ms": 0.0}]
        self._active = None
        self._confirmed = ""
        self._version = 0
        self.played_sample_rate = self.config.playback_sample_rate
        self._hotword_stage = 0
        self.hotword_activations.clear()
        await super().connect()
        if self.hotwords is not None:
            self._record_activation()

    def _hotword_profile(self, stage=None):
        if self.hotwords is not None and hasattr(self.hotwords, "for_stage"):
            return self.hotwords.for_stage(
                self._hotword_stage if stage is None else stage
            )
        return self.hotwords

    def _record_activation(self):
        profile = self._hotword_profile()
        self.hotword_activations.append(
            {
                "stage": self._hotword_stage,
                "played_ms": self.sent_ms,
                "vocabulary": profile.vocabulary(),
                "vocabulary_sha256": profile.vocabulary_sha256,
            }
        )

    async def activate_stage(self, stage):
        """End the prior audio episode before advancing the active vocabulary."""
        if self.hotwords is None or not hasattr(self.hotwords, "for_stage"):
            return
        if stage < self._hotword_stage:
            raise ValueError("Observer hotword stages cannot move backwards")
        self.hotwords.for_stage(stage)
        if stage == self._hotword_stage:
            return
        self.queue_pending()
        self._hotword_stage = stage
        self._record_activation()

    async def send(self, pcm):
        """Accept legacy decoded 8 kHz playback without upsampling it."""
        await self.send_native(pcm, DEFAULT_TELEPHONY_RATE)

    async def send_native(self, pcm, sample_rate):
        """Queue played PCM16 at its original rate, without any conversion."""
        self.drain_failure()
        if not pcm:
            return
        if not isinstance(sample_rate, int) or sample_rate <= 0 or len(pcm) % 2:
            raise ValueError(
                "Native observer requires complete PCM16 samples and a positive rate"
            )
        if sample_rate != self.played_sample_rate:
            self.queue_pending()
            self.played_sample_rate = sample_rate
            # Reviews must never interpret mixed-rate bytes as one recording.
            self._played_pcm.clear()
            self._fragment_ranges.clear()
        start_ms = self.sent_ms
        duration_ms = len(pcm) / (sample_rate * 2) * 1000
        if self._clock_start is None:
            self._clock_start = time.monotonic() - duration_ms / 1000
        self.sent_ms += duration_ms
        if self.config.asr_recheck_enabled:
            self._played_pcm.extend(pcm)
            limit = int(self.config.asr_recheck_buffer_seconds * sample_rate) * 2
            if len(self._played_pcm) > limit:
                del self._played_pcm[:-limit]
            oldest = self.sent_ms - len(self._played_pcm) / (sample_rate * 2) * 1000
            self._fragment_ranges = {
                key: value
                for key, value in self._fragment_ranges.items()
                if value["end_ms"] >= oldest
            }
        speech = audioop.rms(pcm, 2) >= self.config.asr_speech_rms
        if self._active is None:
            if not speech:
                return
            session = _SpeechSession(
                start_ms=start_ms,
                sent_end_ms=start_ms,
                sample_rate=sample_rate,
                hotword_stage=self._hotword_stage
                if hasattr(self.hotwords, "for_stage")
                else None,
                vocabulary=self._hotword_profile().vocabulary()
                if self.hotwords is not None
                else {},
                vocabulary_sha256=(
                    self._hotword_profile().vocabulary_sha256
                    if self.hotwords is not None
                    else hashlib.sha256(b"{}").hexdigest()
                ),
            )
            try:
                self._segments.put_nowait(session)
            except asyncio.QueueFull:
                raise RuntimeError(
                    "Qwen streaming ASR cannot keep up with playback"
                ) from None
            self._active = session
            self.sessions.append(session)
            self._pending_start = start_ms
            self._silence_ms = 0
        self._pending.extend(pcm)
        if speech:
            self._silence_ms = 0
        else:
            self._silence_ms += duration_ms
        max_bytes = round(self.config.asr_append_seconds * sample_rate) * 2
        while len(self._pending) >= max_bytes:
            self._append(max_bytes)
        if self._silence_ms >= self.config.asr_silence_seconds * 1000:
            self.queue_pending()

    def drain_failure(self):
        """Report a failed connection instead of publishing interim text."""
        if self.failure:
            raise RuntimeError(
                "Qwen streaming observer transcription unavailable"
            ) from self.failure

    def _append(self, size):
        pcm = bytes(self._pending[:size])
        end_ms = self._pending_start + len(pcm) / (self._active.sample_rate * 2) * 1000
        try:
            self._active.queue.put_nowait((pcm, self._pending_start, end_ms))
        except asyncio.QueueFull:
            raise RuntimeError(
                "Qwen streaming ASR cannot keep up with playback"
            ) from None
        del self._pending[:size]
        self._pending_start = end_ms

    def queue_pending(self):
        """Finish the played speech episode, including its short final batch."""
        if self._active is None:
            return
        if self._pending:
            self._append(len(self._pending))
        try:
            self._active.queue.put_nowait(None)
        except asyncio.QueueFull:
            raise RuntimeError(
                "Qwen streaming ASR cannot keep up with playback"
            ) from None
        self._active.end_ms = self.sent_ms
        self._active = None
        self._silence_ms = 0

    async def _recognize(self):
        try:
            while True:
                session = await self._segments.get()
                try:
                    await self._run_with_retry(session)
                except Exception:
                    session.status = "failed"
                    raise
                finally:
                    session.replay_audio.clear()
                    self._segments.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Provider error bodies, URLs and exception strings can contain keys.
            self.failure = RuntimeError(
                f"Qwen streaming ASR failed ({type(error).__name__})"
            )

    def _record(self, session, kind, **fields):
        record = {
            "task_id": session.attempts[-1]["task_id"]
            if session.attempts
            else session.task_id,
            "session_id": session.task_id,
            "attempt": len(session.attempts),
            "event": kind,
            "received_ms": (time.monotonic() - self._clock_start) * 1000,
            **fields,
        }
        self.events.append(record)

    async def _run_with_retry(self, session):
        """Replay one episode on a new connection, verifying its released prefix."""
        retries = self.config.asr_streaming_max_retries
        for number in range(retries + 1):
            attempt = {
                "task_id": session.task_id if number == 0 else uuid.uuid4().hex,
                "number": number + 1,
                "status": "running",
            }
            session.attempts.append(attempt)
            session.attempt_next_sentence = 1
            session.attempt_sentences.clear()
            session.sent_end_ms = session.start_ms
            session.usage.clear()
            try:
                await self._run_session(session)
                attempt["status"] = "completed"
                return
            except Exception as error:
                attempt.update(
                    status="failed", reason=type(error).__name__, phase=session.status
                )
                retry = number < retries and _retryable(error)
                if retry and not session.replay_available:
                    self._record(
                        session, "retry_unavailable", reason="audio_buffer_limit"
                    )
                    retry = False
                if not retry:
                    raise
                self._record(session, "retry_scheduled", reason=type(error).__name__)
                session.status = "retrying"
                await asyncio.sleep(self.config.asr_streaming_retry_delay_seconds)
            finally:
                attempt["usage"] = dict(session.usage)

    def _remember_audio(self, session, packet):
        if not session.replay_available or not self.config.asr_streaming_max_retries:
            return
        session.replay_pcm_bytes += len(packet[0])
        limit = self.config.asr_streaming_retry_buffer_seconds * session.sample_rate * 2
        if session.replay_pcm_bytes > limit:
            session.replay_available = False
            session.replay_audio.clear()
        else:
            session.replay_audio.append(packet)

    async def _next_packet(self, session, reader):
        """Wake on disconnect even when no further played audio is arriving."""
        queued = asyncio.create_task(session.queue.get())
        try:
            await asyncio.wait({queued, reader}, return_when=asyncio.FIRST_COMPLETED)
            if not queued.done():
                # The reader has already classified the transport/protocol failure.
                return False, None
            packet = queued.result()
            if packet is None:
                session.input_consumed = True
            else:
                # Retain packets even when the reader fails in the same loop turn.
                self._remember_audio(session, packet)
            return True, packet
        finally:
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)

    async def _run_session(self, session):
        timeout = self.config.asr_streaming_timeout_seconds
        task_id = session.attempts[-1]["task_id"]
        replay = iter(tuple(session.replay_audio))
        parameters = {
            "format": "pcm",
            "sample_rate": session.sample_rate,
            "max_sentence_silence": round(self.config.asr_silence_seconds * 1000),
            "vad_model": "near_meeting_16k",
        }
        if self.language and self.language != "auto":
            parameters["language_hints"] = [self.language]
        if session.vocabulary:
            parameters["vocabulary"] = dict(session.vocabulary)
        request = {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": parameters,
                "input": {},
            },
        }
        session.status = "connecting"
        async with websockets.connect(
            self._stream_url,
            additional_headers={"Authorization": "Bearer " + self._key},
            proxy=None,
            open_timeout=timeout,
            close_timeout=5,
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            started, finished = asyncio.Event(), asyncio.Event()
            failure = []

            async def receive():
                try:
                    async for raw in ws:
                        event = json.loads(raw)
                        kind = event["header"]["event"]
                        if event["header"].get("task_id") != task_id:
                            raise ValueError("ASR task identity mismatch")
                        if kind == "task-failed":
                            raise _ASRTaskFailure(event["header"].get("error_code"))
                        self._process_event(session, event)
                        if kind == "task-started":
                            started.set()
                        elif kind == "task-finished":
                            if session.status != "finishing":
                                raise RuntimeError(
                                    "Qwen ASR completed before finish-task"
                                )
                            finished.set()
                            return
                    raise ConnectionError("Qwen ASR closed before task-finished")
                except Exception as error:
                    failure.append(error)
                    started.set()
                    finished.set()

            reader = asyncio.create_task(receive())
            try:
                await asyncio.wait_for(ws.send(json.dumps(request)), timeout)
                await asyncio.wait_for(started.wait(), timeout)
                if failure:
                    raise failure[0]
                session.status = "running"
                while True:
                    if failure:
                        raise failure[0]
                    item = next(replay, None)
                    if item is None:
                        if session.input_consumed:
                            break
                        available, item = await self._next_packet(session, reader)
                        if not available:
                            raise failure[0]
                    if failure:
                        raise failure[0]
                    if item is None:
                        break
                    pcm, start_ms, end_ms = item
                    # A response may arrive while ws.send is yielding control.
                    # This packet already consists entirely of played audio.
                    session.sent_end_ms = end_ms
                    await asyncio.wait_for(ws.send(pcm), timeout)
                    self.appends.append(
                        {
                            "task_id": task_id,
                            "session_id": session.task_id,
                            "attempt": len(session.attempts),
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                            "sample_rate": session.sample_rate,
                            "pcm_bytes": len(pcm),
                        }
                    )
                session.status = "finishing"
                self._record(session, "finish_requested")
                await asyncio.wait_for(
                    ws.send(
                        json.dumps(
                            {
                                "header": {
                                    "action": "finish-task",
                                    "task_id": task_id,
                                    "streaming": "duplex",
                                },
                                "payload": {"input": {}},
                            }
                        )
                    ),
                    timeout,
                )
                await asyncio.wait_for(finished.wait(), timeout)
                if failure:
                    raise failure[0]
                if any(
                    row["text"].strip()
                    for ident, row in session.attempt_sentences.items()
                    if ident >= session.attempt_next_sentence
                ):
                    raise RuntimeError("Qwen ASR finished with unresolved text")
                if session.attempt_next_sentence < session.next_sentence:
                    raise ValueError("Qwen ASR replay omitted confirmed sentences")
                session.status = "completed"
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    def _process_event(self, session, event):
        """Keep interim evidence, committing only the contiguous final prefix."""
        kind = event["header"]["event"]
        payload = event.get("payload") or {}
        output = payload.get("output") or {}
        if not output.get("sentence") and isinstance(output.get("output"), dict):
            output = output["output"]
        sentence = output.get("sentence")
        usage = payload.get("usage") or output.get("usage") or {}
        session.usage.update(
            {
                key: value
                for key, value in usage.items()
                if key in {"duration", "input_tokens", "output_tokens", "total_tokens"}
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
        )
        if not isinstance(sentence, dict) or sentence.get("heartbeat"):
            self._record(session, kind)
            return
        self._record(
            session,
            kind,
            sentence={
                key: sentence[key]
                for key in (
                    "sentence_id",
                    "text",
                    "sentence_end",
                    "begin_time",
                    "end_time",
                )
                if key in sentence
            },
        )
        ident, text = sentence.get("sentence_id"), sentence.get("text")
        if not isinstance(ident, int) or not isinstance(text, str):
            raise ValueError("Invalid Qwen streaming sentence")
        final = bool(sentence.get("sentence_end"))
        if ident < session.attempt_next_sentence:
            previous = session.attempt_sentences[ident]
            if text.strip() and text.strip() != previous["text"].strip():
                raise ValueError("Qwen changed a previously confirmed sentence")
            return
        session.attempt_sentences[ident] = {
            "text": text,
            "final": final,
            "begin_ms": sentence.get("begin_time"),
            "end_ms": sentence.get("end_time"),
        }
        while (
            row := session.attempt_sentences.get(session.attempt_next_sentence)
        ) and row["final"]:
            ident = session.attempt_next_sentence
            if ident < session.next_sentence:
                # A new socket may revise interim hypotheses. Only compare final
                # sentences, and never publish the already-released prefix twice.
                if row["text"].strip() != session.sentences[ident]["text"].strip():
                    raise ValueError("Qwen ASR replay changed confirmed text")
            else:
                if row["text"].strip():
                    self._commit(session, ident, row)
                session.sentences[ident] = row
                session.next_sentence += 1
            session.attempt_next_sentence += 1

    def _commit(self, session, ident, row):
        start = session.start_ms + (row["begin_ms"] or 0)
        end = (
            session.start_ms + row["end_ms"]
            if row["end_ms"] is not None
            else session.sent_end_ms
        )
        if not session.start_ms <= start <= end <= session.sent_end_ms + 1:
            raise ValueError("Qwen transcript exceeds appended playback")
        end = min(end, session.sent_end_ms)
        text = row["text"].strip()
        event_id = f"qwen-stream-{session.task_id}-{ident}"
        fragment = LiveTranscriptFragment(
            event_id=event_id,
            speaker="agent",
            text=text + " ",
            start_ms=round(start),
            end_ms=round(end),
        )
        self.fragments.put_nowait(fragment)
        self._confirmed = (self._confirmed + " " + text).strip()
        self._version += 1
        self.versions.append(
            {
                "version": self._version,
                "text": self._confirmed,
                "released_ms": (time.monotonic() - self._clock_start) * 1000,
                "event_id": event_id,
            }
        )
        if (
            self.config.asr_recheck_enabled
            and session.sample_rate == self.played_sample_rate
        ):
            self._fragment_ranges[event_id] = {
                **fragment.model_dump(),
                "hotword_stage": session.hotword_stage,
            }
        self.latencies_ms.append(
            max(0, (time.monotonic() - self._clock_start) * 1000 - end)
        )

    def snapshot(self):
        """Return the complete confirmed text, including every final word."""
        self.drain_failure()
        return LiveTranscriptSnapshot(
            version=self._version,
            text=self._confirmed,
            pending=any(session.status != "completed" for session in self.sessions),
        )

    async def flush_pending(self):
        """Finish the remaining speech task and await all final ASR results."""
        self.drain_failure()
        self.queue_pending()
        joined = asyncio.create_task(self._segments.join())
        try:
            await asyncio.wait(
                {joined, self.worker},
                timeout=self.finalization_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            self.drain_failure()
            if not joined.done():
                raise TimeoutError("Qwen streaming ASR finalization timed out")
            return self.drain()
        finally:
            joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)

    @property
    def finalization_timeout_seconds(self):
        """Give retries a bounded drain window shared by the whole backlog."""
        retries = self.config.asr_streaming_max_retries
        # Allow startup/replay plus terminal confirmation for each attempt. Do
        # not multiply by queued episodes: a failing backlog needs a global cap.
        return (retries + 1) * (
            2 * self.config.asr_streaming_timeout_seconds + 5
        ) + retries * self.config.asr_streaming_retry_delay_seconds

    async def recheck(self, evidence, fragments):
        """Use only vocabularies that were active for the referenced audio."""
        stages = [
            row["hotword_stage"]
            for fragment in fragments
            if (row := self._fragment_ranges.get(fragment.get("event_id"))) is not None
            and row.get("hotword_stage") is not None
        ]
        token = self._review_stage.set(max(stages) if stages else self._hotword_stage)
        try:
            return await super().recheck(evidence, fragments)
        finally:
            self._review_stage.reset(token)

    async def _transcribe(self, pcm, *, purpose="recheck"):
        """Use file ASR only for the existing explicit evidence recheck tool."""
        result = await transcribe_qwen(
            self._client,
            self._url,
            DEFAULT_QWEN_ASR_MODEL,
            self._key,
            pcm,
            self.played_sample_rate,
            self.language,
            self._hotword_profile(self._review_stage.get()).vocabulary()
            if self.hotwords is not None
            else None,
        )
        self.usage_records.append({"purpose": purpose, **result.usage})
        return result.text

    def summary(self):
        """Persist session boundaries, raw hypotheses and confirmed text versions."""
        return {
            **super().summary(),
            "mode": "played_audio_streaming",
            "input_sample_rate": self.played_sample_rate,
            "input_sample_rates": sorted(
                {session.sample_rate for session in self.sessions}
            ),
            "audio_processing": "native_pcm_no_resampling",
            "active_hotword_stage": self._hotword_stage,
            "hotword_activations": list(self.hotword_activations),
            "timestamp_source": "provider_sentence_with_playback_offset",
            "latency_basis": "wall_clock_since_played_sentence_end",
            "append_seconds": self.config.asr_append_seconds,
            "tail_words": 0,
            "stable_mode": "ASR_final_confirmation",
            "max_retries": self.config.asr_streaming_max_retries,
            "retry_buffer_seconds": self.config.asr_streaming_retry_buffer_seconds,
            "sessions": [session.model_dump() for session in self.sessions],
            "appends": list(self.appends),
            "events": list(self.events),
            "snapshots": list(self.versions),
            "usage": [
                {
                    "purpose": "primary",
                    "session_id": session.task_id,
                    "task_id": attempt["task_id"],
                    "attempt": attempt["number"],
                    **attempt["usage"],
                }
                for session in self.sessions
                for attempt in session.attempts
                if attempt.get("usage")
            ]
            + list(self.usage_records),
        }

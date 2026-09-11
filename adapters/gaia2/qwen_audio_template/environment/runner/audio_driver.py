"""Headless Gateway client: genuine audio input and timed virtual playback."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from boundary import MeasurementError
from protocol import TERMINAL, Protocol, ProtocolError
from websockets.asyncio.client import connect


class InfrastructureError(RuntimeError):
    def __init__(self, message, cause="adapter_failure"):
        super().__init__(message)
        self.cause = cause


class InputFilteredError(ProtocolError):
    cause = "agent_input_filtered_no_response"


class UserAbort(RuntimeError):
    pass


def service_error_cause(event):
    message = str(event.get("message", ""))
    if re.search(
        r"arrear|account is not in good standing|insufficient.?balance|payment.required|欠费|余额不足",
        message,
        re.IGNORECASE,
    ):
        return "provider_billing"
    if re.search(
        r"unauthorized|invalid.?api.?key|access.?denied|allocationquota|free.*quota.*exceeded",
        message,
        re.IGNORECASE,
    ):
        return "provider_unavailable"
    if re.search(
        r"no (?:user input was received|response was generated) for \d+ seconds",
        message,
        re.IGNORECASE,
    ):
        return "provider_inactivity"
    if "user is speaking" in message.lower():
        return "response_while_speaking"
    return "adapter_failure"


@dataclass
class Playback:
    response_id: str
    chunks: asyncio.Queue = field(default_factory=asyncio.Queue)
    pcm: bytearray = field(default_factory=bytearray)
    sample_rate: int = 24000
    transcript: str | None = None
    ended: bool = False
    cancelled: bool = False
    cancelled_at: float | None = None
    recorded: bool = False
    started: bool = False
    delivery_id: str | None = None
    delivery_kind: str | None = None
    request_state: str | None = None
    generation_done: bool = False
    classification: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    finalizing: bool = False
    played_pcm: bytearray = field(default_factory=bytearray)
    playback_end_sec: float | None = None
    user_context: list = field(default_factory=list)
    work_id: str | None = None


def expected_interruption(
    event: dict, interrupted_at: float | None, now: float
) -> bool:
    """Qwen cancels pending responses when user speech interrupts playback."""
    return (
        event.get("message") == "Realtime 请求已取消"
        and interrupted_at is not None
        and 0 <= now - interrupted_at <= 2
    )


def idle_state(state: dict) -> bool:
    return bool(state.get("ready")) and not any(
        state.get(key, 0)
        for key in (
            "userSpeaking",
            "activeResponses",
            "queuedResponses",
            "pendingPlayback",
            "pendingTasks",
        )
    )


class AudioDriver:
    def __init__(
        self,
        gateway: str,
        token: str,
        audio: Path,
        output: Path,
        notifications,
        clock,
        reply,
        *,
        timeout=2400,
        idle_seconds=15,
        protocol=None,
        barrier=None,
        start_clock=None,
        track="audio",
        text_input=None,
        assessor=None,
        assessment_wait=None,
        stop_file=None,
    ):
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.audio = audio
        self.output = output
        self.notifications = notifications
        self.clock = clock
        self.reply = reply
        self.protocol = protocol or Protocol(output, clock)
        self.barrier = barrier or self.default_barrier
        self.start_clock = start_clock
        self.track = track
        self.text_input = text_input
        self.assessor = assessor
        self.assessment_error = None
        self.assessment_wait = assessment_wait or self.await_assessment
        self.stop_file = Path(stop_file) if stop_file else None
        self.user_context = []
        self.expects_input_transcription = False
        self.pending_input_items = set()
        self.quiet_frames_sent = 0
        self.filtered_input_items = set()
        self.upstream_transcripts = {}
        self.confirmed_filtered_inputs = set()
        self.spoken_context = []
        self.transport_gate = asyncio.Event()
        self.transport_gate.set()
        self.transport_pauses = set()
        self.transport_paused_at = None
        self.transport_paused_sec = 0.0
        self.active_transport = 0
        self.task_activity_serial = 0
        self.recovering_since = None
        self.recoveries = 0
        self.recovery_wait_sec = 0.0
        self.delivery_waiters = {}
        self.delivery_requests = {}
        self.retry_deliveries = set()
        self.input_duration = None
        self.backend_active = False
        self.backend_started = False
        self.last_gateway_request_state = "running"
        self.timeout = timeout
        self.idle_seconds = idle_seconds
        self.responses: dict[str, Playback] = {}
        self.done = False
        self.user_turns = 0
        self.input_done = False
        self.received_response = False
        self.stop_requested = False
        self.last_user_interruption: float | None = None
        self.last_activity = time.monotonic()
        self.session_id = "gaia2-evaluation"
        self.playback_lock = asyncio.Lock()
        self.output.mkdir(parents=True, exist_ok=True)
        self.next_record = 0
        self.background = []

    async def await_assessment(self, task):
        return await task

    def set_transport_paused(self, paused, reason="assessment"):
        if paused:
            self.transport_pauses.add(reason)
        else:
            self.transport_pauses.discard(reason)
        if self.transport_pauses and self.transport_paused_at is None:
            self.transport_paused_at = time.monotonic()
            self.transport_gate.clear()
        elif not self.transport_pauses and self.transport_paused_at is not None:
            self.transport_paused_sec += time.monotonic() - self.transport_paused_at
            self.transport_paused_at = None
            self.transport_gate.set()

    def transport_time(self):
        now = (
            self.transport_paused_at
            if self.transport_paused_at is not None
            else time.monotonic()
        )
        return now - self.transport_paused_sec

    def begin_recovery(self):
        if self.protocol.started_at is not None and (
            not self.input_done or self.pending_input_items
        ):
            raise InfrastructureError(
                "Upstream input interrupted before the complete recording was confirmed; do not score a truncated request",
                "provider_input_interrupted",
            )
        if self.recovering_since is not None:
            return
        self.recovering_since = time.monotonic()
        self.set_transport_paused(True, "reconnect")
        for playback in self.responses.values():
            if not playback.generation_done and not playback.recorded:
                playback.cancelled = True
                playback.cancelled_at = time.monotonic()
                playback.chunks.put_nowait(None)
                if playback.delivery_id in self.delivery_requests:
                    self.retry_deliveries.add(playback.delivery_id)
        for identity, delivery in self.protocol.deliveries.items():
            if (
                not delivery.committed
                and delivery.response_id is None
                and identity in self.delivery_requests
            ):
                self.retry_deliveries.add(identity)

    async def restore_deliveries(self):
        for identity in list(self.retry_deliveries):
            delivery = self.protocol.deliveries[identity]
            self.retry_deliveries.remove(identity)
            if delivery.committed:
                continue
            if int(delivery.attempt_id) >= 2:
                raise InfrastructureError(
                    "Delivery recovery attempt exhausted", "provider_recovery_exhausted"
                )
            delivery.attempt_id = str(int(delivery.attempt_id) + 1)
            delivery.response_id = None
            delivery.cancelled = False
            delivery.status = "queued"
            self.protocol.record(
                "delivery.transport_retry",
                delivery_id=identity,
                attempt_id=delivery.attempt_id,
            )
            await self.control(
                "deliver",
                {
                    **self.delivery_requests[identity],
                    "attemptId": int(delivery.attempt_id),
                },
            )

    async def default_barrier(self, reply, transcript):
        await asyncio.to_thread(reply, transcript)

    async def deliver(self, delivery_id, kind, content):
        delivery = self.protocol.declare(delivery_id, kind)
        if (
            delivery.requested_content is not None
            and delivery.requested_content != content
        ):
            raise ProtocolError("Duplicate delivery changed its requested content")
        delivery.requested_content = content
        if delivery.committed:
            return {"delivery_id": delivery_id, "status": "delivered"}
        if self.track == "backend_text":
            delivery = self.protocol.bind(delivery_id, delivery_id, kind)
            delivery.played = (
                True  # Text control delivery is immediate; explicitly tagged.
            )
            self.protocol.record(
                "text.delivered", delivery_id=delivery_id, content=content
            )
            if self.assessor is None:
                raise MeasurementError("No independent boundary assessor configured")
            task = asyncio.create_task(
                asyncio.to_thread(
                    self.assessor.classify,
                    content,
                    user_context=self.user_context,
                    spoken_context=self.spoken_context,
                )
            )
            decision = await self.assessment_wait(task)
            self.protocol.assess(delivery, decision.kind, decision.to_dict())
            delivery.playback_end_sec = self.protocol.elapsed()
            await self.protocol.commit(delivery, content, self.reply, self.barrier)
            self.spoken_context.append(content)
            return {"delivery_id": delivery_id, "status": "delivered"}
        if delivery_id not in self.delivery_waiters:
            self.protocol.record(
                "delivery.requested",
                delivery_id=delivery_id,
                kind=kind,
                content=content,
            )
            self.delivery_waiters[delivery_id] = (
                asyncio.get_running_loop().create_future()
            )
            payload = {
                "sessionId": self.session_id,
                "deliveryId": delivery_id,
                "kind": kind,
                "content": content,
                "attemptId": 1,
            }
            self.delivery_requests[delivery_id] = payload
            await self.control("deliver", payload)
        return await asyncio.shield(self.delivery_waiters[delivery_id])

    def record(self, direction: str, event: dict) -> None:
        safe = {k: v for k, v in event.items() if k != "audio"}
        row = {"wall_time": time.time(), "direction": direction, **safe}
        with (self.output / "gateway.jsonl").open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def send(self, event: dict) -> None:
        self.record("client", event)
        await self.ws.send(json.dumps(event))

    async def control(self, path: str, payload: dict | None = None) -> dict:
        url = self.gateway + "/api/evaluation/" + path
        if payload is None:
            response = await self.http.get(url, params={"sessionId": self.session_id})
        else:
            response = await self.http.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    async def stream_input(self) -> None:
        with wave.open(str(self.audio), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (
                16000,
                1,
                2,
            ):
                raise InfrastructureError("Expected mono 16 kHz PCM16 input")
            pcm = audio.readframes(audio.getnframes())
            self.input_duration = (
                audio.getnframes() / audio.getframerate()
                if self.track == "audio"
                else 0
            )
        if self.start_clock:
            await asyncio.to_thread(self.start_clock)
        self.protocol.start()
        if self.track == "backend_text":
            self.user_context.append(self.text_input)
            await self.control(
                "backend", {"sessionId": self.session_id, "text": self.text_input}
            )
            self.input_done = True
            return
        if self.track == "system_text":
            await self.send({"type": "text.message", "text": self.text_input})
            self.input_done = True
            return
        # Tail silence is input to the provider's normal VAD, not text fallback.
        pcm += bytes(16000 * 2 * 3 // 2)
        started = self.transport_time()
        for offset in range(0, len(pcm), 640):
            if self.done or self.stop_requested:
                return
            await self.transport_gate.wait()
            await asyncio.sleep(
                max(0, started + offset / 32000 - self.transport_time())
            )
            await self.transport_gate.wait()
            self.active_transport += 1
            try:
                await self.send(
                    {
                        "type": "audio.append",
                        "audio": base64.b64encode(pcm[offset : offset + 640]).decode(),
                    }
                )
            finally:
                self.active_transport -= 1
        self.input_done = True
        self.protocol.record(
            "input.completed",
            audio_duration_sec=self.input_duration,
            tail_silence_sec=1.5,
        )
        self.last_activity = time.monotonic()
        # A live duplex microphone keeps streaming while its speaker is quiet.
        # EOF is not an upstream VAD/ASR end-of-turn event. Keep silence flowing
        # without resetting the meaningful-activity/idle deadline.
        quiet = base64.b64encode(bytes(640)).decode()
        quiet_started = started + len(pcm) / 32000
        while not self.done and not self.stop_requested:
            await self.transport_gate.wait()
            await asyncio.sleep(
                max(
                    0,
                    quiet_started
                    + self.quiet_frames_sent * 0.02
                    - self.transport_time(),
                )
            )
            await self.transport_gate.wait()
            if self.done or self.stop_requested:
                return
            self.active_transport += 1
            try:
                await self.send({"type": "audio.append", "audio": quiet})
                self.quiet_frames_sent += 1
            finally:
                self.active_transport -= 1

    def playback_phase(self, playback, status):
        if playback.delivery_id in self.protocol.deliveries:
            delivery = self.protocol.deliveries[playback.delivery_id]
            if delivery.status != status and not delivery.committed:
                delivery.status = status
                self.protocol.record(
                    "delivery.phase",
                    delivery_id=delivery.delivery_id,
                    response_id=playback.response_id,
                    attempt_id=delivery.attempt_id,
                    status=status,
                )

    def start_assessment(self, playback):
        if playback.generation_done and playback.transcript is not None:
            playback.ready.set()
            if (
                playback.pcm
                and playback.classification is None
                and not playback.cancelled
            ):
                if self.assessor is None:
                    raise MeasurementError(
                        "No independent boundary assessor configured"
                    )
                playback.classification = asyncio.create_task(
                    asyncio.to_thread(
                        self.assessor.classify,
                        playback.transcript,
                        user_context=playback.user_context,
                        spoken_context=list(self.spoken_context),
                    )
                )

                def observe(task):
                    if not task.cancelled() and task.exception() is not None:
                        self.assessment_error = task.exception()

                playback.classification.add_done_callback(observe)
                self.background.append(playback.classification)

    async def maybe_record_reply(self, playback: Playback) -> None:
        self.start_assessment(playback)
        if (
            not playback.ended
            or playback.cancelled
            or playback.recorded
            or playback.finalizing
        ):
            return
        if playback.classification is None:
            return
        playback.finalizing = True
        decision = await self.assessment_wait(playback.classification)
        declared_kind = playback.delivery_kind or "final"
        delivery = self.protocol.bind(
            playback.response_id,
            playback.delivery_id or f"response_{playback.response_id}",
            declared_kind,
        )
        self.protocol.assess(delivery, decision.kind, decision.to_dict())
        delivery.played = True
        delivery.work_id = delivery.work_id or playback.work_id
        delivery.status = "played"
        delivery.playback_end_sec = playback.playback_end_sec
        await self.protocol.commit(
            delivery, playback.transcript, self.reply, self.barrier
        )
        playback.recorded = True
        self.spoken_context.append(playback.transcript)
        self.spoken_context = self.spoken_context[-10:]
        if decision.kind in {"final", "clarification", "failure"}:
            self.protocol.suggest_state(
                {
                    "final": "completed",
                    "clarification": "waiting_user",
                    "failure": "failed",
                }[decision.kind]
            )
        # A model's progress/final hint never overrides the independent decision.
        self.next_record += 1
        self.write_audio(playback, self.next_record)
        waiter = self.delivery_waiters.get(delivery.delivery_id)
        if waiter and not waiter.done():
            await self.control(
                "committed",
                {
                    "sessionId": self.session_id,
                    "deliveryId": delivery.delivery_id,
                    "transcript": playback.transcript,
                    "transcript_scope": "full_generated_response",
                    "heard_transcript": playback.transcript if playback.ended else None,
                },
            )
            waiter.set_result(
                {"delivery_id": delivery.delivery_id, "status": "delivered"}
            )

    def write_audio(self, playback: Playback, number: int) -> None:
        # Numeric paths avoid treating provider response IDs as file paths.
        path = self.output / f"response-{number:04d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(playback.sample_rate)
            wav.writeframes(playback.played_pcm)
        with wave.open(str(path.with_name(path.stem + ".generated.wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(playback.sample_rate)
            wav.writeframes(playback.pcm)
        (path.with_suffix(".json")).write_text(
            json.dumps(
                {
                    "response_id": playback.response_id,
                    "transcript": playback.transcript,
                    "transcript_scope": "full_generated_response",
                    "heard_transcript": playback.transcript if playback.ended else None,
                    "cancelled": playback.cancelled,
                    "played_completely": playback.ended,
                    "received_samples": len(playback.pcm) // 2,
                    "played_samples": len(playback.played_pcm) // 2,
                    "declared_kind": playback.delivery_kind,
                    "playback_end_sec": playback.playback_end_sec,
                },
                ensure_ascii=False,
            )
        )

    async def playback(self, playback: Playback) -> None:
        self.start_assessment(playback)
        async with self.playback_lock:
            while True:
                data = await playback.chunks.get()
                if data is None or playback.cancelled:
                    break
                if not playback.started:
                    playback.started = True
                    self.playback_phase(playback, "playing")
                    await self.send(
                        {"type": "playback.started", "responseId": playback.response_id}
                    )
                # Track only samples actually played, including interruption inside a chunk.
                cursor = 0
                while cursor < len(data) and not playback.cancelled:
                    await self.transport_gate.wait()
                    piece = data[cursor : cursor + playback.sample_rate // 50 * 2]
                    self.active_transport += 1
                    chunk_started = time.monotonic()
                    try:
                        await asyncio.sleep(len(piece) / (2 * playback.sample_rate))
                        played = (
                            len(piece)
                            if not playback.cancelled
                            else min(
                                len(piece),
                                int(
                                    (
                                        (playback.cancelled_at or time.monotonic())
                                        - chunk_started
                                    )
                                    * playback.sample_rate
                                )
                                * 2,
                            )
                        )
                        playback.played_pcm.extend(piece[:played])
                    except asyncio.CancelledError:
                        played = min(
                            len(piece),
                            int(
                                (
                                    (playback.cancelled_at or time.monotonic())
                                    - chunk_started
                                )
                                * playback.sample_rate
                            )
                            * 2,
                        )
                        playback.played_pcm.extend(piece[:played])
                        raise
                    finally:
                        self.active_transport -= 1
                    cursor += len(piece)
            if playback.cancelled:
                self.playback_phase(playback, "cancelled")
                await self.send(
                    {
                        "type": "playback.cancelled",
                        "responseId": playback.response_id,
                        "reason": "provider_cancelled",
                    }
                )
            elif playback.started:
                await self.send(
                    {"type": "playback.ended", "responseId": playback.response_id}
                )
                playback.ended = True
                self.playback_phase(playback, "played")
                playback.playback_end_sec = self.protocol.elapsed()
                self.start_assessment(playback)
                try:
                    await asyncio.wait_for(playback.ready.wait(), timeout=15)
                except TimeoutError as exc:
                    raise InfrastructureError(
                        "Played output has no final transcript/generation confirmation"
                    ) from exc
                await self.maybe_record_reply(playback)
            self.last_activity = time.monotonic()

    def observe_provider_input(self, payload):
        if payload.get("type") in {"session.created", "session.updated"}:
            session = payload.get("session") or {}
            if "input_audio_transcription" in session:
                self.expects_input_transcription = bool(
                    session["input_audio_transcription"]
                )
            return
        identity = payload.get("item_id")
        if not isinstance(identity, str) or not identity:
            return
        kind = payload.get("type")
        if (
            kind == "input_audio_buffer.speech_started"
            and self.expects_input_transcription
        ):
            self.pending_input_items.add(identity)
        elif kind in {
            "conversation.item.input_audio_transcription.completed",
            "conversation.item.ambient_audio_transcription.completed",
        }:
            self.pending_input_items.discard(identity)
        if (
            kind == "input_audio_buffer.speech_stopped"
            and payload.get("reason") == "turn_invalid"
        ):
            self.filtered_input_items.add(identity)
        elif kind == "conversation.item.input_audio_transcription.completed":
            transcript = payload.get("transcript")
            if isinstance(transcript, str) and transcript.strip():
                self.upstream_transcripts[identity] = transcript
        if (
            identity in self.filtered_input_items
            and identity in self.upstream_transcripts
            and identity not in self.confirmed_filtered_inputs
        ):
            self.confirmed_filtered_inputs.add(identity)
            self.protocol.record(
                "input.filtered",
                input_item_id=identity,
                transcript=self.upstream_transcripts[identity],
                added_to_gateway_user_context=False,
            )

    def idle_failure(self):
        if (
            self.track == "audio"
            and not self.user_context
            and not self.received_response
            and not self.backend_started
        ):
            if self.input_done and self.confirmed_filtered_inputs:
                return InputFilteredError(
                    "Complete recording sent; Smart Turn filtered transcribed input and the agent produced no accepted request, action or response"
                )
            return InfrastructureError(
                "No upstream input processing confirmation after complete recording",
                "provider_input_unconfirmed",
            )
        return ProtocolError("Idle without completion, delivery or an environment wait")

    async def receive(self, group: asyncio.TaskGroup) -> None:
        async for raw in self.ws:
            event = json.loads(raw)
            self.record("server", event)
            kind = event.get("type")
            if kind == "evaluation.provider_event":
                payload = event.get("payload", {})
                if (
                    payload.get("type")
                    == "conversation.item.input_audio_transcription.failed"
                ):
                    error = payload.get("error") or {}
                    message = ": ".join(
                        str(error.get(key))
                        for key in ("code", "message")
                        if error.get(key)
                    )
                    cause = service_error_cause({"message": message})
                    self.protocol.record(
                        "input.transcription_failed",
                        input_item_id=payload.get("item_id"),
                        message=message,
                        input_done=self.input_done,
                        quiet_frames_sent=self.quiet_frames_sent,
                    )
                    raise InfrastructureError(
                        message or "Upstream speech transcription failed",
                        cause
                        if cause in {"provider_billing", "provider_unavailable"}
                        else "provider_asr_failure",
                    )
                self.observe_provider_input(payload)
                if payload.get("type") in {
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                    "conversation.item.input_audio_transcription.completed",
                    "conversation.item.ambient_audio_transcription.completed",
                }:
                    self.task_activity_serial += 1
            if kind == "playback.clear" and event.get("reason") == "user_interruption":
                self.last_user_interruption = time.monotonic()
            if kind == "error":
                if expected_interruption(
                    event, self.last_user_interruption, time.monotonic()
                ):
                    continue
                cause = service_error_cause(event)
                if cause == "provider_inactivity":
                    self.begin_recovery()
                    self.protocol.record("connection.recovering", cause=cause)
                    continue
                raise InfrastructureError(event.get("message", "Gateway error"), cause)
            if kind == "voice.ready" and event.get("inputSampleRate", 16000) != 16000:
                raise InfrastructureError(
                    "Gateway input rate differs from packaged audio"
                )
            if kind == "evaluation.connection.recovering":
                self.begin_recovery()
                self.protocol.record("connection.recovering", cause=event.get("reason"))
            if kind == "voice.connection":
                if event.get("state") == "unavailable":
                    if event.get("message"):
                        cause = service_error_cause(event)
                        if cause in {"provider_billing", "provider_unavailable"}:
                            raise InfrastructureError(event["message"], cause)
                    self.begin_recovery()
                elif (
                    event.get("state") == "connected"
                    and self.recovering_since is not None
                ):
                    duration = time.monotonic() - self.recovering_since
                    self.recovery_wait_sec += duration
                    self.recoveries += 1
                    self.protocol.record("connection.recovered", duration_sec=duration)
                    self.recovering_since = None
                    self.set_transport_paused(False, "reconnect")
                    await self.restore_deliveries()
            if kind == "evaluation.delivery.injected":
                delivery = self.protocol.deliveries.get(event.get("deliveryId"))
                if delivery:
                    delivery.work_id = event.get("workId")
                self.protocol.record(
                    "delivery.injected",
                    delivery_id=event.get("deliveryId"),
                    work_id=event.get("workId"),
                    context_injected=event.get("contextInjected"),
                    requested_content=event.get("content"),
                    supplied_context=event.get("suppliedContext"),
                )
            if kind == "task.accepted":
                self.backend_started = True
            if kind == "transcript.final" and event.get("role") == "user":
                self.user_context.append(event.get("content", "")[:1000])
                self.user_context = self.user_context[-10:]
                self.protocol.record(
                    "input.segment",
                    segment_id=event.get("turnId"),
                    content=event.get("content", ""),
                )
            if kind in {
                "audio.delta",
                "transcript.final",
                "task.accepted",
                "task.completed",
                "task.failed",
                "voice.connection",
                "evaluation.connection.recovering",
            }:
                self.task_activity_serial += 1
            response_id = event.get("responseId")
            if (
                kind
                in {
                    "response.started",
                    "audio.delta",
                    "transcript.final",
                    "audio.done",
                    "evaluation.delivery",
                }
                and response_id
            ):
                playback = self.responses.setdefault(
                    response_id,
                    Playback(response_id, user_context=list(self.user_context)),
                )
                if event.get("taskId"):
                    playback.work_id = event["taskId"]
                if event.get("deliveryId"):
                    playback.delivery_id = event["deliveryId"]
                    existing = self.protocol.deliveries.get(playback.delivery_id)
                    if existing and not playback.cancelled:
                        self.protocol.bind(
                            playback.response_id,
                            playback.delivery_id,
                            existing.declared_kind,
                        )
                self.last_activity = time.monotonic()
                if kind == "audio.delta":
                    if not playback.started:
                        self.playback_phase(playback, "generating")
                    self.received_response = True
                    pcm = base64.b64decode(event["audio"], validate=True)
                    if len(pcm) % 2:
                        raise InfrastructureError("Misaligned PCM16 output")
                    rate = int(event.get("sampleRate") or 24000)
                    if playback.pcm and rate != playback.sample_rate:
                        raise InfrastructureError(
                            "Output rate changed within a response"
                        )
                    playback.sample_rate = rate
                    first = not playback.pcm
                    playback.pcm.extend(pcm)
                    playback.chunks.put_nowait(pcm)
                    if first and pcm:
                        self.background.append(
                            group.create_task(self.playback(playback))
                        )
                elif kind == "transcript.final" and event.get("role") == "assistant":
                    playback.transcript = event.get("content", "")
                    self.start_assessment(playback)
                elif kind == "evaluation.delivery":
                    playback.delivery_id = event["deliveryId"]
                    playback.delivery_kind = event["kind"]
                    playback.request_state = event.get("requestState")
                    if event.get("cancelled"):
                        playback.cancelled = True
                        playback.cancelled_at = time.monotonic()
                        delivery = self.protocol.deliveries.get(playback.delivery_id)
                        if delivery:
                            delivery.cancelled = True
                        waiter = self.delivery_waiters.get(playback.delivery_id)
                        if waiter and not waiter.done():
                            waiter.set_exception(
                                ProtocolError(
                                    "Formal delivery cancelled before confirmation"
                                )
                            )
                    self.start_assessment(playback)
                elif kind == "audio.done":
                    playback.generation_done = True
                    playback.chunks.put_nowait(None)
                    self.start_assessment(playback)
            if kind in {"playback.clear", "response.interrupted"}:
                for playback in self.responses.values():
                    if not playback.ended and (
                        not response_id or playback.response_id == response_id
                    ):
                        playback.cancelled = True
                        playback.cancelled_at = time.monotonic()
                        if playback.delivery_id in self.protocol.deliveries:
                            self.protocol.deliveries[
                                playback.delivery_id
                            ].cancelled = True
                        waiter = self.delivery_waiters.get(playback.delivery_id)
                        if waiter and not waiter.done():
                            waiter.set_exception(
                                ProtocolError("Formal delivery was interrupted")
                            )
                        playback.chunks.put_nowait(None)
        if not self.done:
            raise InfrastructureError(
                "Gateway connection closed before trial completion"
            )

    async def advance_observation(self, state):
        raise NotImplementedError("Only observation drivers can advance a window")

    async def drive(self, group: asyncio.TaskGroup) -> None:
        ready_deadline = time.monotonic() + 180
        while True:
            if self.stop_file and self.stop_file.exists():
                raise UserAbort("Evaluation stopped during startup")
            state = await self.control("state")
            if state.get("ready"):
                break
            if time.monotonic() >= ready_deadline:
                raise InfrastructureError("Realtime frontend did not become ready")
            await asyncio.sleep(0.25)
        idle_since = None
        while not self.done:
            if self.stop_file and self.stop_file.exists():
                if self.assessor and hasattr(self.assessor, "cancel"):
                    self.assessor.cancel()
                raise UserAbort("Evaluation stopped by user")
            failures = self.output / "model-requests/provider-failures.jsonl"
            if failures.exists() and failures.stat().st_size:
                raise InfrastructureError(
                    "Backend provider or frozen-model profile failed; see trusted provider-failures.jsonl",
                    "backend_provider_failure",
                )
            if self.assessment_error is not None:
                raise self.assessment_error
            if (
                self.recovering_since is not None
                and time.monotonic() - self.recovering_since > 30
            ):
                raise InfrastructureError(
                    "Realtime recovery exceeded 30 seconds",
                    "provider_recovery_exhausted",
                )
            epoch, rate = await asyncio.to_thread(self.clock)
            await self.control("clock", {"epochMs": epoch * 1000, "rate": rate})
            for message in await asyncio.to_thread(self.notifications):
                kind = message["type"]
                if kind == "USER_MESSAGE":
                    self.user_turns += 1
                    if self.user_turns != 1:
                        raise InfrastructureError(
                            "No audio asset for additional user message"
                        )
                    self.background.append(group.create_task(self.stream_input()))
                elif kind == "ENVIRONMENT_NOTIFICATION":
                    if self.protocol.notify(message) and not self.backend_active:
                        await self.control(
                            "notification", {**message, "sessionId": self.session_id}
                        )
                        self.protocol.consumed_notifications.add(message["id"])
                elif kind == "ENVIRONMENT_STOP":
                    self.stop_requested = True
            state = await self.control("state")
            if state.get("backendError"):
                raise InfrastructureError(state["backendError"])
            backend_result = state.get("backendResult")
            if (
                self.track == "backend_text"
                and backend_result
                and not getattr(self, "backend_result_seen", False)
            ):
                self.backend_result_seen = True
                metadata = backend_result.get("metadata", {})
                if not metadata.get("evaluationDelivered"):
                    await self.deliver(
                        "backend-return", "final", backend_result["content"]
                    )
                self.protocol.set_state(metadata.get("evaluationState", "completed"))
            if self.stop_requested:
                self.done = True
            else:
                self.backend_active = bool(state.get("pendingTasks"))
                if not self.backend_active and self.protocol.state not in TERMINAL:
                    for identity, notification in self.protocol.notifications.items():
                        if identity not in self.protocol.consumed_notifications:
                            await self.control(
                                "notification",
                                {**notification, "sessionId": self.session_id},
                            )
                            self.protocol.consumed_notifications.add(identity)
                            self.protocol.record(
                                "notification.consumed",
                                notification_id=identity,
                                consumer="frontend",
                            )
                declared = state.get("requestState", "running")
                if declared != self.last_gateway_request_state:
                    self.last_gateway_request_state = declared
                    self.protocol.set_state(declared)
                delivered_ids = state.get("claimedDeliveryIds", []) + (
                    backend_result or {}
                ).get("metadata", {}).get("evaluationDeliveryIds", [])
                if any(
                    identity not in self.protocol.deliveries
                    or not self.protocol.deliveries[identity].committed
                    for identity in delivered_ids
                ):
                    raise ProtocolError(
                        "Coordinator claimed an unconfirmed delivery ID"
                    )
                if (
                    self.input_done
                    and idle_state(state)
                    and time.monotonic() - self.last_activity >= self.idle_seconds
                ):
                    for playback in self.responses.values():
                        if (
                            playback.ended
                            and not playback.cancelled
                            and (
                                playback.transcript is None
                                or (
                                    playback.delivery_kind is None
                                    and not getattr(self, "observation_mode", False)
                                )
                            )
                        ):
                            raise InfrastructureError(
                                "Playback is missing confirmed transcript or delivery metadata"
                            )
                local_pending = bool(self.pending_input_items) or any(
                    not p.recorded and not p.cancelled and p.pcm
                    for p in self.responses.values()
                )
                idle = idle_state(state) and not local_pending
                if getattr(self, "observation_mode", False):
                    self.done = await self.advance_observation(state)
                elif self.protocol.can_finish(input_done=self.input_done, idle=idle):
                    self.done = True
                elif (
                    self.input_done
                    and idle
                    and self.protocol.state != "waiting_environment"
                ):
                    idle_since = idle_since or time.monotonic()
                    if (
                        time.monotonic() - max(idle_since, self.last_activity)
                        >= self.idle_seconds
                    ):
                        raise self.idle_failure()
                else:
                    idle_since = None
            if (self.protocol.elapsed() or 0) >= self.timeout:
                raise TimeoutError(
                    "Qwen audio execution exceeded its wall-clock budget"
                )
            await asyncio.sleep(0.25)

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(
            headers=headers, timeout=15, trust_env=False
        ) as self.http:
            if self.track == "backend_text":
                await self.control("backend-session", {"sessionId": self.session_id})
                try:
                    async with asyncio.TaskGroup() as group:
                        try:
                            await self.drive(group)
                        finally:
                            self.done = True
                            for task in self.background:
                                task.cancel()
                finally:
                    self.done = True
                return
            epoch, rate = await asyncio.to_thread(self.clock)
            await self.control("clock", {"epochMs": epoch * 1000, "rate": rate})
            url = self.gateway.replace("http://", "ws://").replace("https://", "wss://")
            async with connect(
                url + "/api/realtime?sessionId=" + self.session_id,
                origin=self.gateway,
                additional_headers=headers,
                max_size=4 * 1024 * 1024,
                proxy=None,
            ) as self.ws:
                await self.send(
                    {
                        "type": "connect",
                        "inputEnabled": True,
                        "outputEnabled": True,
                        "voiceEnabled": True,
                        "textOnly": False,
                        "timeZone": "UTC",
                        "locale": "en-US",
                    }
                )
                try:
                    async with asyncio.TaskGroup() as group:
                        self.background.append(group.create_task(self.receive(group)))
                        try:
                            await self.drive(group)
                        finally:
                            self.done = True
                            for task in self.background:
                                task.cancel()
                finally:
                    self.done = True
                    for playback in self.responses.values():
                        if not playback.recorded and playback.pcm:
                            self.next_record += 1
                            self.write_audio(playback, self.next_record)

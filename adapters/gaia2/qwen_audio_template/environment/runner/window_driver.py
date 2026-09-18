"""V4 driver: playback receipts are independent of the one official submission."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, override

import httpx
from audio_driver import AudioDriver, idle_state
from boundary import MeasurementError
from observation import VERSION, IdleWindow, project_audio
from protocol import Protocol

if TYPE_CHECKING:
    from runtime import Runtime


class IncompleteAudibleTranscript(MeasurementError):
    cause = "audible_transcript_incomplete"


class WindowProtocol(Protocol):
    version = VERSION

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.end_reason: str | None = None
        self.end_sec: float | None = None
        self.audible_complete: bool | None = None

    @override
    def assess(self, delivery, kind, evidence):
        # Model labels are telemetry only, never a semantic judgement.
        delivery.kind = kind
        self.record(
            "delivery.label_recorded",
            delivery_id=delivery.delivery_id,
            declared_kind=delivery.declared_kind,
            origin="model",
        )

    @override
    async def commit(self, delivery, transcript, reply, barrier):
        if delivery.cancelled or not delivery.played:
            return False
        if delivery.committed:
            if delivery.transcript != transcript:
                raise ValueError("Duplicate playback receipt changed text")
            return False
        delivery.transcript = transcript
        delivery.committed = True
        delivery.status = "committed"
        self.last_delivery = delivery.playback_end_sec
        self.record(
            "delivery.played",
            delivery_id=delivery.delivery_id,
            response_id=delivery.response_id,
            transcript=transcript,
            declared_kind=delivery.declared_kind,
            playback_end_sec=delivery.playback_end_sec,
            are_turn=None,
        )
        return True

    @override
    def metrics(self):
        result = super().metrics()
        result.pop("final_formal_delivery_sec")
        result.pop("last_formal_delivery_sec")
        result.update(
            last_audio_playback_end_sec=self.last_delivery
            if self.audible_complete is not False
            else None,
            final_answer_verified=None,
            observation_end_reason=self.end_reason,
            observation_end_sec=self.end_sec,
        )
        return result


class WindowDriver(AudioDriver):
    observation_mode = True

    def __init__(self, *args, protocol: Protocol, **kwargs):
        super().__init__(*args, protocol=protocol, **kwargs)
        if not isinstance(protocol, WindowProtocol):
            raise TypeError("V4 driver requires a WindowProtocol")
        self.observation = protocol
        if self.track != "audio":
            raise ValueError("V4 initially supports the audio track only")
        self.window = IdleWindow(self.idle_seconds)
        self._runtime: Runtime | None = None

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            raise RuntimeError("Observation runtime is not attached")
        return self._runtime

    @runtime.setter
    def runtime(self, value: Runtime):
        self._runtime = value

    @override
    def start_assessment(self, playback):
        if playback.generation_done and playback.transcript is not None:
            playback.ready.set()
            if (
                playback.classification is None
                and playback.pcm
                and not playback.cancelled
            ):
                future = asyncio.get_running_loop().create_future()
                future.set_result(
                    SimpleNamespace(
                        kind=playback.delivery_kind or "progress",
                        to_dict=lambda: {
                            "origin": "model",
                            "semantic_assessment": False,
                        },
                    )
                )
                playback.classification = future

    async def proxy_state(self):
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            response = await client.get(
                "http://127.0.0.1:8001/__state",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            return response.json()

    def locally_idle(self, state, proxy):
        return (
            self.input_done
            and idle_state(state)
            and not self.pending_input_items
            and self.recovering_since is None
            and not proxy["active"]
            and not self.runtime.waiting
            and not self.runtime.operations.locked()
            and not any(
                k not in self.protocol.consumed_notifications
                for k in self.protocol.notifications
            )
            and not any(
                not d.committed and not d.cancelled
                for d in self.protocol.deliveries.values()
            )
            and not any(
                not p.recorded and not p.cancelled and p.pcm
                for p in self.responses.values()
            )
        )

    @override
    async def advance_observation(self, state):
        proxy = await self.proxy_state()
        activity = (
            self.last_activity,
            self.task_activity_serial,
            self.protocol.sequence,
            proxy["sequence"],
        )
        if not self.window.update(
            time.monotonic(), idle=self.locally_idle(state, proxy), activity=activity
        ):
            return False
        # Close admission then recheck: a completion hint alone never closes a window.
        self.runtime.dispatch.clear()
        await self.control("barrier", {"paused": True})
        await self.runtime.proxy_barrier(True)
        after = await self.control("state")
        after_proxy = await self.proxy_state()
        if not self.locally_idle(after, after_proxy) or activity != (
            self.last_activity,
            self.task_activity_serial,
            self.protocol.sequence,
            after_proxy["sequence"],
        ):
            await self.control("barrier", {"paused": False})
            await self.runtime.proxy_barrier(False)
            self.runtime.dispatch.set()
            self.window.reset()
            return False
        await self.freeze("idle_window")
        return True

    async def freeze(self, reason):
        self.runtime.accepting = False
        self.runtime.dispatch.set()  # Wake waiters to reject rather than deadlock.
        await self.control("barrier", {"paused": True})
        async with self.runtime.operations:
            with self.runtime.tick_lock:
                self.runtime.env.pause()
                self.observation.end_reason = reason
                self.observation.end_sec = self.protocol.elapsed()
        self.protocol.record("observation.ended", reason=reason)

    @override
    async def run(self):
        timeout = None
        try:
            await super().run()
        except* TimeoutError as exc:
            timeout = exc
            self.runtime.accepting = False
            self.runtime.env.pause()
            self.observation.end_reason = "total_timeout"
            self.observation.end_sec = self.protocol.elapsed()
            self.protocol.record("observation.ended", reason="total_timeout")
        if self.observation.end_reason is None:
            self.runtime.accepting = False
            self.runtime.env.pause()
            self.observation.end_reason = "scenario_end"
            self.observation.end_sec = self.protocol.elapsed()
            self.protocol.record("observation.ended", reason="scenario_end")
        self.finalize_projection()
        if timeout:
            raise timeout

    def finalize_projection(self):
        projection = project_audio(self.output)
        self.observation.audible_complete = projection["audible_transcript_complete"]
        projection["observation_end_sec"] = self.observation.end_sec
        (self.output / "projection.json").write_text(
            json.dumps(projection, indent=2) + "\n"
        )
        if not projection["audible_transcript_complete"]:
            raise IncompleteAudibleTranscript(
                "Actual played speech cannot be fully projected"
            )
        if projection["text"]:
            with self.runtime.tick_lock:
                self.reply(projection["text"])
            self.protocol.turn = 1
            self.protocol.record(
                "observation.submitted",
                text_sha256=projection["text_sha256"],
                are_turn=1,
                segment_count=len(projection["segments"]),
            )

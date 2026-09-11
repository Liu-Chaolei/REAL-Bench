"""Real-time ARE callbacks and serial formal-delivery boundaries."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from are.simulation.apps.system import SystemApp
from boundary import MeasurementError


class AssessmentBusy(RuntimeError):
    """Normal task activity prevents excluding this grading interval."""


class Runtime:
    def __init__(self, env, protocol):
        self.env = env
        self.protocol = protocol
        self.operations = asyncio.Lock()
        self.tick_lock = threading.RLock()
        self.dispatch = asyncio.Event()
        self.dispatch.set()
        self.driver = None
        self.waiting = False
        self.accepting = True
        self.original_tick = env.tick
        env.tick = self.tick
        env.pause()
        system = env.get_app_with_class(SystemApp)
        if system is not None:
            system.wait_for_next_notification = self.wait_for_notification

    def tick(self):
        with self.tick_lock:
            if self.env.is_paused():
                return None
            return self.original_tick()

    def start(self):
        with self.tick_lock:
            # Preparation, MCP discovery and realtime handshake are not task time.
            self.env.time_manager.reset(start_time=self.env.start_time)
            self.env.time_manager.pause()
            self.env.resume()

    def wait_for_notification(self):
        system = self.env.get_app_with_class(SystemApp)
        timeout = system.wait_for_notification_timeout
        deadline = (
            timeout.timeout_timestamp if timeout else self.env.time_manager.time()
        )
        self.waiting = True
        self.protocol.record("wait.started", deadline=deadline)
        try:
            # The normal environment loop continues executing events. No offset,
            # queue fast-forward or paused-clock work is permitted here.
            while not self.env.stop_event.is_set():
                if any(
                    key not in self.protocol.consumed_notifications
                    for key in self.protocol.notifications
                ):
                    break
                if self.env.time_manager.time() >= deadline:
                    break
                time.sleep(0.025)
        finally:
            self.waiting = False
            # Official tick handles the timeout notification and reset normally.
            self.protocol.record("wait.ended")

    async def proxy_barrier(self, paused):
        token = os.environ.get("QWAUDIO_EVALUATION_TOKEN")
        if not token:
            return
        async with httpx.AsyncClient(
            timeout=600 if paused else 15, trust_env=False
        ) as client:
            response = await client.post(
                "http://127.0.0.1:8001/__barrier",
                json={"paused": paused},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()

    @asynccontextmanager
    async def assessment_scope(self):
        """Exclude only proven exclusive grading pauses; fail closed on overlap."""
        self.dispatch.clear()
        paused = False
        serial = None
        try:
            if self.driver:
                await self.driver.control("barrier", {"paused": True})
            await self.proxy_barrier(
                True
            )  # Drain already dispatched inference, counting its real cost.
            async with self.operations:
                if self.driver:
                    state = await self.driver.control("state")
                    if not state.get("ready"):
                        raise MeasurementError("Assessment gateway is not ready")
                    if (
                        state.get("userSpeaking")
                        or state.get("activeResponses")
                        or getattr(self.driver, "pending_input_items", ())
                    ):
                        raise AssessmentBusy("Speech/inference is still active")
                    self.driver.set_transport_paused(True)
                    while self.driver.active_transport:
                        await asyncio.sleep(0.005)
                    serial = self.driver.task_activity_serial
                with self.tick_lock:
                    self.env.pause()
                    paused = True
                self.protocol.record("assessment.pause.started")
                try:
                    yield
                finally:
                    self.protocol.record("assessment.pause.ended")
                    if self.driver and serial != self.driver.task_activity_serial:
                        raise MeasurementError(
                            "Task activity advanced inside an excluded grading pause"
                        )
        finally:
            # Exceptions before env.pause must not resume a clock we did not pause.
            if paused:  # ty: ignore[redundant-condition-strict]
                self.env.resume()
            if self.driver:
                self.driver.set_transport_paused(False)
                await self.driver.control("barrier", {"paused": False})
            await self.proxy_barrier(False)
            self.dispatch.set()

    async def await_assessment(self, task):
        if task.done():
            return task.result()
        try:
            async with self.assessment_scope():
                return await task
        except AssessmentBusy:
            # Scope cleanup has released all dispatch/transport gates. Concurrent
            # task activity is legitimate; keep real time running rather than
            # inventing an exclusive pause or rejecting a valid interaction.
            self.protocol.record("assessment.concurrent_wait.started")
            try:
                return await task
            finally:
                self.protocol.record("assessment.concurrent_wait.ended")

    async def barrier(self, reply, transcript):
        self.dispatch.clear()
        if self.driver:
            await self.driver.control("barrier", {"paused": True})
        try:
            await self.proxy_barrier(True)
            async with self.operations:
                condition_prefix = f"condition_turn_{self.protocol.turn + 1}-CHECK_"
                needs_check = any(
                    e.event_id.startswith(condition_prefix)
                    for e in self.env.event_queue.list_view()
                )

                if self.driver:
                    state = await self.driver.control("state")
                    if (
                        not state.get("ready")
                        or state.get("userSpeaking")
                        or state.get("activeResponses")
                    ):
                        raise MeasurementError(
                            "Cannot establish formal turn barrier during active speech/inference"
                        )
                    self.driver.set_transport_paused(True)
                    while self.driver.active_transport:
                        await asyncio.sleep(0.005)

                def commit():
                    with self.tick_lock:
                        reply(transcript)
                        # Execute normal registered turn conditions, including
                        # failure retention and placeholder release, exactly once.
                        self.original_tick()

                await asyncio.to_thread(commit)
                # Conditions may already be scheduled for the next normal tick.
                # Hold dispatch until that check actually completes, not merely
                # until send_message_to_user has returned.
                while needs_check and not self.env.stop_event.is_set():
                    checked = any(
                        e.event_id.startswith(condition_prefix)
                        and e.metadata.return_value is True
                        for e in self.env.event_log.list_view()
                    )
                    if checked:
                        break
                    await asyncio.sleep(0.025)
        finally:
            if self.driver:
                self.driver.set_transport_paused(False)
                await self.driver.control("barrier", {"paused": False})
            await self.proxy_barrier(False)
            self.dispatch.set()

    async def call(self, name, tool, arguments):
        await self.dispatch.wait()
        async with self.operations:
            if not self.accepting or self.env.stop_event.is_set():
                raise RuntimeError("Scenario is no longer running")
            started_sec = self.protocol.elapsed()
            try:
                result = await asyncio.to_thread(tool, **arguments)
            except Exception:
                self.protocol.action(
                    name,
                    False,
                    application=not isinstance(tool.class_instance, SystemApp),
                )
                raise
            self.protocol.action(
                name,
                True,
                application=not isinstance(tool.class_instance, SystemApp),
                started_sec=started_sec,
            )
            if tool.name == "wait_for_notification":
                return {
                    "result": result,
                    "notifications": self.protocol.take_notifications(),
                }
            return result

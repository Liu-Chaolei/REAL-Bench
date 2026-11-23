"""Authenticated bridge transport, independent of Qwen's model protocol."""

import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from queue import Empty, Full, Queue

import websockets

from tau2.config import DEFAULT_QWEN_AGENT_BACKEND_EVENT_QUEUE_SIZE
from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime


class QwenAgentProvider:
    """Keep a continuous event receiver while the orchestrator executes tools."""

    def __init__(self, config, trace_path: Path | None = None, runtime=None):
        self.config = config
        self.runtime = runtime or DockerRuntime(config)
        if isinstance(self.runtime, DockerRuntime) and trace_path:
            self.runtime.diagnostics_path = trace_path.with_suffix(".container.log")
        self.trace_path = trace_path
        self.session_id = str(uuid.uuid4())
        self.events = asyncio.Queue(maxsize=10000)
        self.ws = None
        self.reader = None
        self.failure = None
        self.metadata = {}
        self.input_sample_rate = None
        self.diagnostics_enabled = False
        self.backend_inputs = {}
        self.state_observations = []
        self.backend_events = Queue(maxsize=DEFAULT_QWEN_AGENT_BACKEND_EVENT_QUEUE_SIZE)
        self.persistent_tasks_enabled = False
        self.backend_capabilities = None
        self.backend_evidence_gap = False
        self.backend_event_callback = None
        self.backend_observer_ready = False
        self._trace = None

    @property
    def is_connected(self):
        """Whether both bridge transport and event receiver are healthy."""
        return self.ws is not None and self.failure is None

    def record(self, event):
        """Persist diagnostic events without copying raw media or credentials."""
        if self.diagnostics_enabled and event.get("type") == "diagnostic_backend_ready":
            self.backend_observer_ready = True
        if event.get("type") == "backend_observer_ready":
            self.backend_capabilities = event.get("capabilities")
        is_reply = event.get("type") == "gateway_event" and (
            event.get("event") or {}
        ).get("type") in {
            "transcript.delta",
            "transcript.final",
            "response.audio_transcript.delta",
            "response.audio_transcript.done",
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
            "response.created",
            "response.started",
        }
        event.setdefault("received_monotonic", time.monotonic())
        if self.diagnostics_enabled and event.get("type") in {
            "backend_task_state",
            "backend_native_operation",
        }:
            self.state_observations.append(dict(event))

        if is_reply or event.get("type") in {
            "diagnostic_backend",
            "backend_task_state",
            "backend_native_operation",
        }:
            if self.diagnostics_enabled and event.get("type") == "diagnostic_backend":
                self.backend_inputs[event.get("id", str(len(self.backend_inputs)))] = {
                    k: v
                    for k, v in event.items()
                    if k not in {"type", "received_monotonic"}
                }
            if self.persistent_tasks_enabled:
                payload = dict(
                    event, session_id=event.get("session_id") or self.session_id
                )
                if isinstance(self.runtime, DockerRuntime):
                    payload = json.loads(self.runtime.redact(json.dumps(payload)))
                try:
                    self.backend_events.put_nowait(payload)
                except Full:
                    self.backend_evidence_gap = True
        if self._trace:
            safe = {k: v for k, v in event.items() if k not in {"audio", "token"}}
            content = json.dumps({"time": time.monotonic(), **safe})
            if isinstance(self.runtime, DockerRuntime):
                content = self.runtime.redact(content)
            self._trace.write(content + "\n")
            self._trace.flush()

    async def connect(self, system_prompt, tools):
        """Boot one isolated system with public policy and tool schemas only."""
        self.failure = None
        self.events = asyncio.Queue(maxsize=10000)
        self.backend_events = Queue(maxsize=DEFAULT_QWEN_AGENT_BACKEND_EVENT_QUEUE_SIZE)
        self.backend_capabilities = None
        self.backend_evidence_gap = False
        self.backend_inputs = {}
        self.state_observations = []
        self.backend_observer_ready = False
        self.session_id = str(uuid.uuid4())
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.trace_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            os.fchmod(descriptor, 0o600)
            self._trace = os.fdopen(descriptor, "w")
        try:
            url = await asyncio.to_thread(self.runtime.start)
            deadline = (
                asyncio.get_running_loop().time() + self.config.boot_timeout_seconds
            )
            while True:
                try:
                    self.ws = await websockets.connect(
                        url,
                        additional_headers={
                            "Authorization": f"Bearer {self.runtime.token}"
                        },
                        max_size=16 * 1024 * 1024,
                        proxy=None,
                    )
                    break
                except (OSError, websockets.exceptions.InvalidMessage):
                    # Docker can accept TCP before the bridge has bound its
                    # endpoint. No model session has started at this point.
                    if isinstance(self.runtime, DockerRuntime):
                        await asyncio.to_thread(self.runtime.check_running)
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError("Qwen bridge did not start")
                    await asyncio.sleep(0.1)
            await self.send(
                {
                    "type": "initialize",
                    "protocol": 1,
                    "session_id": self.session_id,
                    "policy": system_prompt,
                    "tools": [tool.openai_schema["function"] for tool in tools],
                    "diagnostics_enabled": self.diagnostics_enabled,
                    "persistent_tasks_enabled": self.persistent_tasks_enabled,
                    "tool_timeout_ms": round(self.config.tool_timeout_seconds * 1000),
                }
            )
            remaining = max(0.1, deadline - asyncio.get_running_loop().time())
            async with asyncio.timeout(remaining):
                while True:
                    event = json.loads(await self.ws.recv())
                    self.record(event)
                    if event.get("type") == "error":
                        raise RuntimeError(event.get("message", "Qwen boot failed"))
                    if event.get("type") == "ready":
                        if event.get("protocol") != 1:
                            raise ValueError("Incompatible Qwen bridge protocol")
                        self.input_sample_rate = int(event["input_sample_rate"])
                        self.metadata = {
                            **self.runtime.metadata,
                            **event.get("metadata", {}),
                        }
                        if isinstance(self.runtime, DockerRuntime):
                            proof = self.metadata.get("sandbox", {})
                            if (
                                proof.get("mechanism") != "mount_namespace"
                                or proof.get("version") != 1
                                or proof.get("capabilities") != "none"
                                or not proof.get("cross_directory_rename")
                                or not proof.get("cross_directory_hardlink")
                                or not {
                                    "child_denied",
                                    "view_locked",
                                    "proc_root_denied",
                                }.issubset(proof.get("checks", []))
                            ):
                                raise RuntimeError(
                                    "Missing effective confinement proof"
                                )
                        self.record({"type": "provenance", **self.metadata})
                        if (
                            self.persistent_tasks_enabled
                            and not self.backend_capabilities
                        ):
                            raise RuntimeError(
                                "Qwen image does not support persistent backend observation"
                            )
                        break
            self.reader = asyncio.create_task(self._read())
        except BaseException:
            await self.disconnect()
            raise

    async def next_event(self):
        """Yield transport events continuously, propagating connection failure."""
        while True:
            if self.failure is not None:
                raise self.failure
            try:
                return await asyncio.wait_for(self.events.get(), 0.1)
            except asyncio.TimeoutError:
                continue

    async def _read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                self.record(event)
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message", "Qwen bridge error"))
                if event.get("type", "").startswith("diagnostic_backend") or event.get(
                    "type"
                ) in {
                    "backend_task_state",
                    "backend_native_operation",
                    "backend_observer_ready",
                }:
                    continue
                self.events.put_nowait(event)
            raise RuntimeError("Qwen bridge disconnected during the call")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc

    async def send(self, event):
        """Send one ordered control or media event; never replay failed writes."""
        if self.failure:
            raise RuntimeError("Qwen bridge failed") from self.failure
        await self.ws.send(json.dumps(event))

    def drain(self):
        """Drain received events without waiting for backend work to finish."""
        if self.failure:
            raise RuntimeError("Qwen bridge failed") from self.failure
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def drain_backend_events(self):
        """Drain backend observations without injecting them into media events."""
        events = []
        while True:
            try:
                events.append(self.backend_events.get_nowait())
            except Empty:
                break
        return events

    def set_backend_event_callback(self, callback):
        """Attach a passive backend observer used by persistent-task evaluation."""
        self.backend_event_callback = callback

    async def disconnect(self):
        """Close sockets and the owned container even after partial startup."""
        try:
            if self.reader:
                self.reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.reader
            if self.ws:
                await self.ws.close()
        finally:
            self.ws = None
            try:
                await asyncio.to_thread(self.runtime.close)
            finally:
                if self._trace:
                    self._trace.close()
                    self._trace = None

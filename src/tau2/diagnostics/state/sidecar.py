"""Asynchronous full-state sidecar for state interaction diagnostics."""

from __future__ import annotations

import copy
import queue
import threading
from pathlib import Path
from typing import Any

from tau2.diagnostics.intent.io import write_json
from tau2.diagnostics.model import RequestBudget, TauJsonModel
from tau2.diagnostics.state.adapter import DomainAdapter
from tau2.diagnostics.state.models import DialogueEvent
from tau2.diagnostics.state.projector import StateProjector
from tau2.diagnostics.state.semantic import SemanticInterpreter


class StateCheckpointStore:
    """Persist ordered events and complete per-event state checkpoints."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.checkpoints = self.directory / "checkpoints"
        self.requests = self.directory / "requests"
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        self.requests.mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def record_event(self, event: DialogueEvent) -> None:
        with self._lock:
            self.events.append(event.model_dump(mode="json"))

    def write_checkpoint(self, result, event: DialogueEvent) -> None:
        payload = {
            "sequence": event.sequence,
            "event_id": event.event_id,
            "event": event.model_dump(mode="json"),
            "state_before": result.state_before.model_dump(mode="json"),
            "interpretation": result.interpretation.model_dump(mode="json"),
            "state_after": result.state_after.model_dump(mode="json"),
            "transition": (
                result.transition.model_dump(mode="json")
                if result.transition is not None
                else None
            ),
            "status": "complete",
        }
        target = self.checkpoints / f"{event.sequence:06d}.json"
        temporary = target.with_suffix(".json.writing")
        write_json(temporary, payload)
        temporary.replace(target)

    def write_status(self, **values: Any) -> None:
        write_json(self.directory / "status.json", values)

    def write_trace(self, projector: StateProjector):
        windows, versions, state = projector.finish()
        trace = {
            "version": "tau-dialogue-state/2",
            "events": self.events,
            "versions": versions,
            "transitions": [w.model_dump(mode="json") for w in windows],
            "final_state": state.model_dump(mode="json"),
        }
        write_json(self.directory / "state_trace.json", trace)
        return windows, versions, state


class StateShadowWorker:
    """Process a trial's state events in order outside the main simulation."""

    def __init__(self, task, evidence, config, directory: Path, capabilities=None):
        self.task = task
        self.evidence = evidence
        self.config = config
        self.store = StateCheckpointStore(Path(directory))
        self.queue: queue.Queue[DialogueEvent | None] = queue.Queue(
            maxsize=getattr(config, "state_sidecar_queue_size", 256)
        )
        self.interpreter = SemanticInterpreter(
            TauJsonModel(
                config,
                "state_interaction",
                self.store.requests,
                RequestBudget(config.max_requests),
            ),
            capabilities or {},
        )
        self.projector = StateProjector(
            task,
            interpreter=self.interpreter,
            adapter=DomainAdapter(evidence),
            evidence=evidence,
        )
        self.thread = threading.Thread(
            target=self._run, name="tau-state-sidecar", daemon=True
        )
        self.received_sequence = -1
        self.committed_sequence = -1
        self.failed_events: list[dict] = []
        self._closed = False
        self._started = False
        self._error: Exception | None = None

    def start(self) -> None:
        if not self._started:
            self._started = True
            self.thread.start()

    def submit(self, event: dict | DialogueEvent) -> None:
        if self._closed:
            return
        value = (
            event
            if isinstance(event, DialogueEvent)
            else DialogueEvent.model_validate(copy.deepcopy(event))
        )
        self.received_sequence = max(self.received_sequence, value.sequence)
        self.store.record_event(value)
        try:
            self.queue.put_nowait(value)
        except queue.Full:
            self.failed_events.append(
                {
                    "sequence": value.sequence,
                    "event_id": value.event_id,
                    "reason": "sidecar_queue_full",
                }
            )

    def _run(self) -> None:
        pending: dict[int, DialogueEvent] = {}
        next_sequence = 0
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                pending[item.sequence] = item
                while next_sequence in pending:
                    event = pending.pop(next_sequence)
                    try:
                        result = self.projector.process_event(event)
                        self.store.write_checkpoint(result, event)
                        self.committed_sequence = event.sequence
                    except Exception as exc:
                        self.failed_events.append(
                            {
                                "sequence": event.sequence,
                                "event_id": event.event_id,
                                "reason": type(exc).__name__,
                            }
                        )
                    next_sequence += 1
                self.store.write_status(
                    received_sequence=self.received_sequence,
                    committed_sequence=self.committed_sequence,
                    status="running",
                    failed_events=self.failed_events,
                )
            if pending:
                self.failed_events.append(
                    {
                        "reason": "missing_event_sequence",
                        "sequences": sorted(pending),
                    }
                )
        except Exception as exc:
            self._error = exc
        finally:
            self.store.write_status(
                received_sequence=self.received_sequence,
                committed_sequence=self.committed_sequence,
                status="failed" if self._error else "draining",
                failed_events=self.failed_events,
            )

    def close_and_drain(self, timeout: float = 900.0) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            self.start()
        self.queue.put(None)
        self.thread.join(timeout=max(0.0, timeout))
        if self.thread.is_alive():
            self.store.write_status(
                received_sequence=self.received_sequence,
                committed_sequence=self.committed_sequence,
                status="incomplete",
                failed_events=self.failed_events,
            )
            return
        windows, _, _ = self.store.write_trace(self.projector)
        from tau2.diagnostics.state.metrics import result_from_windows

        write_json(
            self.store.directory / "state_interaction.json",
            result_from_windows(
                windows,
                trace_path="state_trace.json",
                status="partial" if self.failed_events else None,
            ),
        )
        status = "complete" if not self.failed_events else "partial"
        self.store.write_status(
            received_sequence=self.received_sequence,
            committed_sequence=self.committed_sequence,
            status=status,
            failed_events=self.failed_events,
            windows=len(windows),
        )

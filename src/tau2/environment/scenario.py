"""Shared scenario clock, causal events and replayable execution journal."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from tau2.config import (
    DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS,
    DEFAULT_SCENARIO_POLL_SECONDS,
)
from tau2.data_model.scenario import ScenarioRecord, ScenarioSpec

_REFERENCE = re.compile(r"\{\{([^{}]+)\}\}")


def resolve_references(value: Any, results: dict[str, Any]) -> Any:
    """Resolve explicit result references without evaluating dataset strings."""
    if isinstance(value, list):
        return [resolve_references(item, results) for item in value]
    if isinstance(value, dict):
        return {key: resolve_references(item, results) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    match = _REFERENCE.fullmatch(value)
    if match:
        return copy.deepcopy(results[match[1]])
    return _REFERENCE.sub(lambda m: str(results[m[1]]), value)


def equivalent_arguments(actual: Any, expected: Any) -> bool:
    """Compare structural anchor arguments, tolerating harmless text spacing."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and equivalent_arguments(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(equivalent_arguments(a, b) for a, b in zip(actual, expected))
        )
    if isinstance(expected, str) and isinstance(actual, str):
        return (
            " ".join(actual.split()).casefold() == " ".join(expected.split()).casefold()
        )
    return actual == expected


class ScenarioRuntime:
    """Drive declarative events independently of model and audio work.

    The runtime knows operations and observations, never a task reward. A single
    reentrant lock serializes world mutations. Waiting tools must release it.
    Offline replay uses the same operations with a manually advanced clock.
    """

    def __init__(
        self,
        spec: ScenarioSpec,
        execute: Callable[[str, dict], Any],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        managed_user_events: set[str] | None = None,
    ):
        self.event_triggers = None
        self.spec = spec
        self.managed_user_events = managed_user_events or set()
        self.execute = execute
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.records: list[ScenarioRecord] = []
        self.results: dict[str, Any] = {}
        self.completed: dict[str, float] = {}
        self._event_ordinals = {event.id: i for i, event in enumerate(spec.events)}
        self.notifications: list[dict] = []
        self.user_inputs: list[tuple[str, str]] = []
        self.user_input_metadata: dict[str, dict[str, str | None]] = {}
        self.failure: BaseException | None = None
        self._origin: float | None = None
        self._manual: float | None = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drained = 0
        self.replaying = False
        self._operation_time = ContextVar(
            f"scenario_operation_{id(self)}", default=None
        )
        self._released_inputs: set[str] = set()
        self._agent_text = ""
        self._agent_fragments = []
        self._observations = []
        self._reference_tools = {
            event.tool
            for event in spec.events
            if event.kind == "reference" and event.channel == "tool"
        }
        self._observation_sequence = 0
        self._operation_matches = {}
        self._operation_decisions = {}
        self.semantic_matcher = None
        self.argument_normalizer = lambda name, arguments: arguments
        self._match_executor = None
        self._matches = {}
        self._match_candidates = {}
        self._needed_anchors = {
            p
            for e in spec.events
            if e.kind != "reference" and e.id not in self.managed_user_events
            for p in e.dependencies
        }
        self._event_completion_listeners: list[Callable[[str, Any, float], None]] = []
        self._turn_boundary_listeners: list[Callable[[float], None]] = []
        self._delivery_listeners: list[
            Callable[[str, str, str | None, float], None]
        ] = []
        for _ in spec.events:
            before = len(self._needed_anchors)
            self._needed_anchors.update(
                p
                for e in spec.events
                if e.id in self._needed_anchors
                for p in e.dependencies
            )
            if before == len(self._needed_anchors):
                break

    def time(self) -> float:
        """Return the authoritative scenario timestamp, not host calendar time."""
        fixed = self._operation_time.get()
        if fixed is not None:
            return fixed
        elapsed = (
            self._manual
            if self._manual is not None
            else self.monotonic() - self._origin
        )
        return self.spec.start_time + elapsed

    @contextmanager
    def operation(self):
        """Give one atomic operation a reproducible application timestamp."""
        with self.lock:
            started = self.time()
            token = self._operation_time.set(started)
            try:
                yield started
            finally:
                self._operation_time.reset(token)

    @property
    def elapsed(self) -> float:
        """Seconds since the first delivered user input."""
        return self.time() - self.spec.start_time

    @property
    def started(self) -> bool:
        """Whether real input delivery has started the scenario clock."""
        return self._origin is not None

    def timestamp_at(self, monotonic_time: float) -> float:
        """Map a physical playback time onto the authoritative scenario clock."""
        if self._origin is None:
            raise ValueError("The scenario clock has not started")
        return self.spec.start_time + monotonic_time - self._origin

    def start(self, *, monotonic_origin: float | None = None) -> None:
        """Start real time after startup and speech preparation have completed."""
        with self.lock:
            if self._origin is not None:
                return
            self._origin = (
                self.monotonic() if monotonic_origin is None else monotonic_origin
            )
            self._manual = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(DEFAULT_SCENARIO_POLL_SECONDS):
            try:
                self.advance()
            except BaseException as error:
                self.failure = error
                self.record(
                    "error", name="scheduler", result=type(error).__name__, error=True
                )
                self._stop.set()
                with self.changed:
                    self.changed.notify_all()

    def close(self) -> None:
        """Stop only this trial's scheduler and wake outstanding waiters."""
        self._stop.set()
        with self.changed:
            self.changed.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        if self._match_executor is not None:
            self._match_executor.shutdown(wait=False, cancel_futures=True)

    def record(self, kind: str, **fields) -> ScenarioRecord:
        """Append one immutable, serializable journal record."""
        with self.lock:
            event_time = fields.pop("event_time", None)
            record = ScenarioRecord(
                sequence=len(self.records),
                time=self.time() if event_time is None else event_time,
                kind=kind,
                observed_time=self.time() if event_time is not None else None,
                **fields,
            )
            # Reject opaque/non-replayable results at their point of origin.
            json.dumps(record.model_dump(mode="json"), allow_nan=False)
            self.records.append(record)
            self.changed.notify_all()
            return record

    def observe(
        self, name: str, arguments: dict, result: Any, *, error=False, call_id=None
    ) -> None:
        """Bind successful actual effects once, including effects observed early."""
        if error or name not in self._reference_tools:
            return
        with self.lock:
            self._observation_sequence += 1
            if self.event_triggers is not None and not self.replaying:
                self.event_triggers.observe(
                    name,
                    arguments,
                    result,
                    self.elapsed,
                    call_id or f"observation{self._observation_sequence}",
                )
            self._observations.append(
                (
                    name,
                    copy.deepcopy(arguments),
                    copy.deepcopy(result),
                    self.elapsed,
                    call_id or f"observation{self._observation_sequence}",
                )
            )
            self._bind_observations()
            if not self.replaying:
                self.advance()

    def _bind_tool(self, event, observation):
        name, arguments, result, when, identifier = observation
        completed = max(
            when,
            max((self.completed[parent] for parent in event.dependencies), default=0),
        )
        self.completed[event.id] = completed
        self.results[event.id] = copy.deepcopy(result)
        self.record(
            "anchor",
            event_id=event.id,
            result=result,
            arguments={"call_id": identifier},
            started_time=self.spec.start_time + completed,
        )
        self._observations = [
            item for item in self._observations if item[4] != identifier
        ]
        for listener in list(self._event_completion_listeners):
            listener(event.id, copy.deepcopy(result), self.spec.start_time + completed)

    def add_event_completion_listener(
        self, listener: Callable[[str, Any, float], None]
    ) -> None:
        """Notify a sidecar when an original reference tool event binds."""
        with self.lock:
            self._event_completion_listeners.append(listener)

    def add_turn_boundary_listener(self, listener: Callable[[float], None]) -> None:
        """Register a callback invoked at a communication boundary."""
        with self.lock:
            self._turn_boundary_listeners.append(listener)

    def notify_turn_boundary(self, timestamp: float | None = None) -> None:
        """Let sidecars release requests after the current turn is complete."""
        with self.lock:
            when = self.time() if timestamp is None else timestamp
            for listener in list(self._turn_boundary_listeners):
                listener(when)

    def add_delivery_listener(
        self, listener: Callable[[str, str, str | None, float], None]
    ) -> None:
        """Register a callback for actual user/agent speech delivery."""
        with self.lock:
            self._delivery_listeners.append(listener)

    def queue_user_input(
        self,
        event_id: str,
        text: str,
        *,
        source: str = "scenario",
        purpose: str = "request",
    ) -> None:
        """Queue a user request while retaining its mechanism provenance."""
        with self.lock:
            self.user_inputs.append((event_id, text))
            self.user_input_metadata[event_id] = {
                "source": source,
                "purpose": purpose,
            }

    def _match_text_fields(self, pairs):
        return all(
            self.semantic_matcher(actual, expected) for actual, expected in pairs
        )

    def event_priority(self, event):
        """Order eligible events by causal due time and stable source ordinal.

        Reference timing only disambiguates identical observations; it never
        imposes a minimum execution time on an agent's successful operation.
        """
        return (
            self.event_triggers.due(event)
            if self.event_triggers is not None
            and event.id in self.event_triggers.managed
            and self.event_triggers.ready(event)
            else max((self.completed[p] for p in event.dependencies), default=0)
            + event.delay_seconds,
            self._event_ordinals[event.id],
        )

    def _bind_observations(self):
        """Match actual effects without blocking media on textual equivalence."""
        changed = True
        while changed:
            changed = False
            eligible = [
                event
                for event in self.spec.events
                if event.kind == "reference"
                and event.channel == "tool"
                and event.id not in self.completed
                and all(parent in self.completed for parent in event.dependencies)
            ]
            for event in sorted(eligible, key=self.event_priority):
                try:
                    full_expected = resolve_references(event.arguments, self.results)
                except KeyError:
                    continue
                expected = (
                    full_expected
                    if event.match_fields is None
                    else {
                        key: value
                        for key, value in full_expected.items()
                        if key in event.match_fields
                    }
                )
                for observation in list(self._observations):
                    name, arguments, result, when, identifier = observation
                    if name != event.tool or not equivalent_arguments(
                        self.argument_normalizer(name, arguments),
                        self.argument_normalizer(name, expected),
                    ):
                        continue
                    pairs = []
                    impossible = False
                    for field in event.semantic_fields:
                        left = arguments.get(field)
                        right = full_expected.get(field)
                        left = "" if left is None else left
                        right = "" if right is None else right
                        if equivalent_arguments(left, right):
                            continue
                        if not isinstance(left, str) or not isinstance(right, str):
                            impossible = True
                            break
                        pairs.append((left, right))
                    if impossible:
                        continue
                    if pairs:
                        key = (event.id, identifier)
                        if self._operation_decisions.get(key) is False:
                            continue
                        if key not in self._operation_decisions:
                            if self.semantic_matcher is None or self.replaying:
                                continue
                            if key not in self._operation_matches:
                                if self._match_executor is None:
                                    self._match_executor = ThreadPoolExecutor(
                                        max_workers=1, thread_name_prefix="tau-evidence"
                                    )
                                self._operation_matches[key] = (
                                    self._match_executor.submit(
                                        self._match_text_fields, pairs
                                    )
                                )
                            continue
                    self._bind_tool(event, observation)
                    changed = True
                    break
                if changed:
                    break  # A newly eligible child may precede another anchor.

    def delivered(
        self,
        kind: str,
        text: str,
        *,
        event_id: str | None = None,
        delivered_at: float | None = None,
    ) -> None:
        """Observe actual speech delivery; future captions never count."""
        with self.lock:
            metadata = self.user_input_metadata.get(
                event_id or "",
                {"source": "scenario", "purpose": "scripted_request"},
            )
            self.record(
                kind,
                result=text,
                event_id=event_id,
                event_time=delivered_at,
                source=metadata.get("source") if kind == "user" else None,
                purpose=metadata.get("purpose") if kind == "user" else None,
            )
            when = self.time() if delivered_at is None else delivered_at
            for listener in list(self._delivery_listeners):
                listener(kind, text, event_id, when)
            if kind == "user" and event_id and event_id not in self.completed:
                self.completed[event_id] = (
                    self.elapsed
                    if delivered_at is None
                    else delivered_at - self.spec.start_time
                )
                self.results[event_id] = None
                self._bind_observations()
            if kind == "agent":
                self._agent_text += " " + text
                self._agent_fragments.append(
                    (
                        text,
                        self.elapsed
                        if delivered_at is None
                        else delivered_at - self.spec.start_time,
                    )
                )
                if not self.replaying:
                    self._observe_agent()
            if not self.replaying:
                self.advance()

    def advance(self, elapsed: float | None = None) -> None:
        """Execute due environment events or release a scripted user message."""
        with self.lock:
            if self.failure:
                raise RuntimeError("Scenario scheduler failed") from self.failure
            if self.event_triggers is not None and not self.replaying:
                self.event_triggers.advance()
            for key, future in list(self._operation_matches.items()):
                if future.done():
                    self._operation_decisions[key] = future.result()
                    del self._operation_matches[key]
                    self._bind_observations()
            finished = False
            for key, (future, delivered_at) in list(self._matches.items()):
                if future.done():
                    if future.result() and key not in self.completed:
                        self._bind_agent(key, delivered_at)
                    del self._matches[key]
                    finished = True
            if finished and not self.replaying:
                self._observe_agent()
            if elapsed is not None:
                if self._origin is not None:
                    raise ValueError("Cannot fast-forward a running real-time scenario")
                if elapsed < self._manual:
                    raise ValueError("Scenario time cannot move backwards")
                self._manual = elapsed
            progress = True
            while progress:
                progress = False
                eligible = [
                    event
                    for event in self.spec.events
                    if event.kind != "reference"
                    and event.id not in self.managed_user_events
                    and event.id not in self.completed
                    and (
                        self.event_triggers.ready(event)
                        if self.event_triggers is not None
                        and event.id in self.event_triggers.managed
                        else all(p in self.completed for p in event.dependencies)
                    )
                ]
                for event in sorted(eligible, key=self.event_priority):
                    due, _ = self.event_priority(event)
                    if self.elapsed < due:
                        continue
                    arguments = (
                        self.event_triggers.arguments(event)
                        if self.event_triggers is not None
                        and event.id in self.event_triggers.managed
                        else resolve_references(event.arguments, self.results)
                    )
                    if event.kind == "user":
                        if event.id not in self._released_inputs:
                            self.queue_user_input(
                                event.id,
                                arguments["content"],
                                source="scenario",
                                purpose="scripted_request",
                            )
                            self._released_inputs.add(event.id)
                        continue  # Delivery, rather than release, completes input.
                    with self.operation() as started:
                        result = self.execute(event.tool, arguments)
                    self.completed[event.id] = self.elapsed
                    self.results[event.id] = copy.deepcopy(result)
                    record = self.record(
                        "environment",
                        name=event.tool,
                        arguments=arguments,
                        result=result,
                        event_id=event.id,
                        started_time=started,
                    )
                    self.notifications.append(
                        {"time": record.time, "operation": event.tool, "result": result}
                    )
                    self.changed.notify_all()
                    progress = True
                    break  # Recompute priority after a parent changes readiness.

    def _bind_agent(self, key, delivered_at):
        self.completed[key] = delivered_at
        self.results[key] = None
        self.record(
            "anchor", event_id=key, started_time=self.spec.start_time + delivered_at
        )

    def _observe_agent(self):
        """Match causal speech asynchronously, leaving the media loop unblocked."""
        for event in self.spec.events:
            if (
                event.kind != "reference"
                or event.channel != "agent"
                or event.id in self.completed
            ):
                continue
            if not all(p in self.completed for p in event.dependencies):
                continue
            try:
                expected = str(
                    resolve_references(event.arguments, self.results)["content"]
                )
            except KeyError:
                continue
            eligible = max((self.completed[p] for p in event.dependencies), default=0)
            fragments = [
                (text, when) for text, when in self._agent_fragments if when >= eligible
            ]
            candidate = " ".join(text for text, _ in fragments)
            delivered_at = max((when for _, when in fragments), default=self.elapsed)
            if not candidate:
                continue
            if (
                " ".join(expected.split()).casefold()
                in " ".join(candidate.split()).casefold()
            ):
                self._bind_agent(event.id, delivered_at)
            elif (
                self.semantic_matcher is not None
                and event.id in self._needed_anchors
                and event.id not in self._matches
                and self._match_candidates.get(event.id) != candidate
            ):
                if self._match_executor is None:
                    self._match_executor = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="tau-evidence"
                    )
                self._match_candidates[event.id] = candidate
                self._matches[event.id] = (
                    self._match_executor.submit(
                        self.semantic_matcher, candidate, expected
                    ),
                    delivered_at,
                )

    def drain(self) -> list[ScenarioRecord]:
        """Return journal records not yet attached to a trajectory tick."""
        with self.lock:
            result = self.records[self._drained :]
            self._drained = len(self.records)
            return result

    def wait_for_notification(
        self, timeout: float = DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS
    ) -> list[dict]:
        """Wait without holding the mutation lock or stopping audio ticks."""
        deadline = self.monotonic() + timeout
        with self.changed:
            while not self.notifications and not self._stop.is_set():
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    break
                self.changed.wait(min(remaining, 0.1))
            if self.failure:
                raise RuntimeError("Scenario scheduler failed") from self.failure
            result, self.notifications = self.notifications, []
            return result

"""Optional semantic and heartbeat watchdog for long-running simulations."""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.data_model.message import SystemMessage, UserMessage
from tau2.data_model.semantic_communication import SemanticCommunicateSettings
from tau2.utils.llm_utils import extract_json_from_llm_response, generate


class WatchdogVerdict(BaseModel):
    """Structured result of one watchdog inspection."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal[
        "continue", "wait_one_more_checkpoint", "stop_and_evaluate", "uncertain"
    ]
    state: Literal[
        "progressing",
        "expected_wait",
        "backend_pending",
        "agent_repeating",
        "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def normalize_common_model_variants(cls, value):
        """Accept harmless judge synonyms while keeping one internal schema."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        decisions = {
            "stop": "stop_and_evaluate",
            "stop_and_grade": "stop_and_evaluate",
            "wait": "wait_one_more_checkpoint",
            "keep_waiting": "wait_one_more_checkpoint",
            "proceed": "continue",
        }
        states = {
            "in_progress": "progressing",
            "progress": "progressing",
            "waiting": "expected_wait",
            "pending": "backend_pending",
            "stuck": "agent_repeating",
        }
        if normalized.get("decision") in decisions:
            normalized["decision"] = decisions[normalized["decision"]]
        if normalized.get("state") in states:
            normalized["state"] = states[normalized["state"]]
        evidence = normalized.get("evidence", [])
        if isinstance(evidence, dict):
            normalized["evidence"] = [
                f"{key}={value}" for key, value in evidence.items()
            ]
        elif isinstance(evidence, str):
            normalized["evidence"] = [evidence]
        return normalized


WATCHDOG_SYSTEM_PROMPT = """You are a runtime watchdog for a full-duplex simulation.

Your only responsibility is to detect persistent deadlock or unproductive loops.
Normal task completion and stage transitions are handled by the simulation.

DEFAULT DECISION
Choose continue unless the supplied evidence establishes persistent deadlock or
an unproductive loop. If a problem is suspected but not established, choose
wait_one_more_checkpoint. If essential information is missing or contradictory,
choose uncertain. Both choices allow the simulation to continue.

MULTI-STAGE TASKS
current_stage is zero-based; total_stages describes the whole task. Unreleased
stages are not failed or abandoned stages. Allow the current interaction to
finish and the normal stage controller to release later requests.

A moment with no active speech, pending tool result, or pending transition does
not establish that stage progression is stuck. transition_pending=false and
awaiting_delivery=false do not mean that no later requests remain.

CLARIFICATION AND ORDINARY WAITING
Continue when the user is asking for clarification, repetition, spelling,
verification, or confirmation and the exchange is still making substantive
progress. New information, a relevant correction, a partial but advancing
answer, or a meaningfully different recovery attempt is progress.

A repeated question is not automatically a loop: ASR errors, partial spelling,
interrupted playback, or an incomplete answer can justify asking again. A live
tool/API request, retry, audio generation, playback, ASR confirmation, stage
transition, declared notification wait, or task-required delay can also justify
waiting. Check its timing and actual progress rather than its mere existence.

An active user turn with status "speaking" or is_final=false means the user is
still talking. Continue and wait for that turn to finish. Do not stop during an
incomplete utterance or while fresh speech or other substantive activity shows
that the interaction is progressing.

STOP CONDITIONS
Choose stop_and_evaluate only when evidence establishes one of these conditions:
1. Persistent deadlock: a necessary operation is demonstrably stuck or cannot
   complete, and no reasonable wait, retry, recovery, or alternative action is
   currently progressing.
2. Persistent unproductive loop: the agent, the user, or both repeatedly traverse
   the same substantive sequence without adding useful information, resolving an
   outstanding question, completing an action, or making a meaningful recovery.

In either case, all of the following must hold:
- Observations cover distinct moments or repeated complete cycles, not a single
  instantaneous observation.
- The lack of progress persists across those observations.
- Ordinary waiting, clarification, active speech, and recovery do not adequately
  explain the behavior.
- You can identify the specific blocked operation or repeating cycle.

A long duration, an unchanged final answer, repeated status checks, an empty
tool queue, or silence alone is insufficient. Outstanding stages neither prove
a loop nor prevent stopping a genuinely established loop. An overdue request
needs evidence of persistent blocking, not just one late response observation.

INTERPRETING THE INPUT
Use only the supplied evidence. Missing fields mean unknown, not inactive.
The input is an event summary, not a raw tick log. Completed turns are historical
records. Repeated transcript snapshots or delayed ASR releases are not repeated
spoken utterances. Confirmed ASR fragments are recorded once, when released.
Missing transcript text does not prove silence; check live speech/output flags.

Dialogue timestamps use the host wall clock or explicitly marked ASR release
times; application operations may use the scenario clock. Compare timestamps
only when their clock bases are compatible and use the supplied current-time
references. Reaching a watchdog checkpoint is not a deadline violation.

A live heartbeat shows runtime responsiveness, not necessarily business
progress. A stale heartbeat is a diagnostic clue, not sufficient evidence by
itself that the participants are trapped in a loop.

OUTPUT
Return JSON only, with exactly these five fields: decision, state, confidence,
reason, evidence. Use these values exactly; do not invent synonyms:
- decision: continue, wait_one_more_checkpoint, stop_and_evaluate, uncertain.
- state: progressing, expected_wait, backend_pending, agent_repeating, unknown.
- confidence: a number from 0 to 1.
- reason: one short string.
- evidence: an array of at most 8 short strings, never an object or nested data.

Use agent_repeating for an established unproductive conversational cycle
involving either participant. For stop_and_evaluate, evidence must identify the
blocked operation or repeating cycle and observations showing its persistence.
Confidence describes confidence in that runtime failure, not confidence that
the business work is complete. Do not add other fields.
"""


class WatchdogController:
    """Run bounded LLM checks and monitor the orchestrator heartbeat."""

    def __init__(
        self,
        config,
        judge: SemanticCommunicateSettings | None = None,
    ):
        self.config = config
        self.judge_settings = judge or SemanticCommunicateSettings()
        self.started_at = time.monotonic()
        self.last_heartbeat = self.started_at
        self.last_phase = "initializing"
        self.phase_started = self.started_at
        self.last_tick = 0
        self.stale = False
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._judge_thread: threading.Thread | None = None
        self._judge_queue: queue.Queue[WatchdogVerdict] = queue.Queue(maxsize=1)
        self.checkpoints: list[dict] = []

    def heartbeat(self, tick: int, phase: str) -> None:
        """Record a small in-memory heartbeat; no per-tick file is written."""
        if self.stale:
            return
        if phase != self.last_phase:
            self.phase_started = time.monotonic()
        self.last_heartbeat = time.monotonic()
        self.last_tick = tick
        self.last_phase = phase
        self.stale = False

    def start(self, on_stale: Callable[[], None]) -> None:
        """Start the daemon heartbeat monitor when watchdog mode is enabled."""
        if not self.config.enabled or self._heartbeat_thread is not None:
            return

        def monitor() -> None:
            interval = min(1.0, self.config.heartbeat_timeout_seconds / 3)
            while not self._stop_event.wait(interval):
                age = time.monotonic() - self.last_heartbeat
                if age > self.config.heartbeat_timeout_seconds:
                    if not self.stale:
                        self.stale = True
                        on_stale()

        self._heartbeat_thread = threading.Thread(
            target=monitor,
            name="tau2-watchdog-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop(self) -> None:
        """Stop the daemon monitor during finalization."""
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)

    def _judge_once(self, context: str) -> None:
        try:
            response = generate(
                model=self.judge_settings.model,
                messages=[
                    SystemMessage(role="system", content=WATCHDOG_SYSTEM_PROMPT),
                    UserMessage(role="user", content=context),
                ],
                call_name="watchdog_health_check",
                response_model=WatchdogVerdict,
                response_validation_retries=1,
                **self.judge_settings.arguments(),
            )
            verdict = WatchdogVerdict.model_validate_json(
                extract_json_from_llm_response(response.content or "{}")
            )
        except Exception as error:
            verdict = WatchdogVerdict(
                decision="uncertain",
                state="unknown",
                confidence=0.0,
                reason=f"Watchdog judge unavailable: {type(error).__name__}",
                evidence=[],
            )
        try:
            self._judge_queue.put_nowait(verdict)
        except queue.Full:
            pass

    def judge(self, context: str) -> WatchdogVerdict:
        """Run one bounded judge call; failures conservatively continue."""
        if self._judge_thread is not None and self._judge_thread.is_alive():
            return WatchdogVerdict(
                decision="uncertain",
                state="unknown",
                confidence=0.0,
                reason="Previous watchdog request is still pending",
                evidence=[],
            )
        while not self._judge_queue.empty():
            try:
                self._judge_queue.get_nowait()
            except queue.Empty:
                break
        self._judge_thread = threading.Thread(
            target=self._judge_once,
            args=(context,),
            name="tau2-watchdog-judge",
            daemon=True,
        )
        previous_phase = self.last_phase
        self.heartbeat(self.last_tick, "watchdog_judge")
        self._judge_thread.start()
        deadline = time.monotonic() + self.config.judge_timeout_seconds
        try:
            while self._judge_thread.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._judge_thread.join(
                    timeout=min(remaining, self.config.heartbeat_timeout_seconds / 3)
                )
                # The main thread is alive inside its own bounded inspection.
                self.heartbeat(self.last_tick, "watchdog_judge")
        finally:
            self.heartbeat(self.last_tick, previous_phase)
        try:
            return self._judge_queue.get_nowait()
        except queue.Empty:
            return WatchdogVerdict(
                decision="uncertain",
                state="unknown",
                confidence=0.0,
                reason="Watchdog judge timed out",
                evidence=[],
            )

    def record(self, tick: int, verdict: WatchdogVerdict, action: str) -> None:
        self.checkpoints.append(
            {
                "tick": tick,
                "phase": self.last_phase,
                "decision": verdict.decision,
                "state": verdict.state,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "evidence": verdict.evidence,
                "action": action,
            }
        )

    def summary(self) -> dict:
        """Return serializable watchdog provenance for SimulationRun.info."""
        return {
            "enabled": bool(self.config.enabled),
            "initial_check_ticks": self.config.initial_check_ticks,
            "check_interval_ticks": self.config.check_interval_ticks,
            "max_total_ticks": self.config.max_total_ticks,
            "last_tick": self.last_tick,
            "last_phase": self.last_phase,
            "phase_age_seconds": round(time.monotonic() - self.phase_started, 3),
            "heartbeat_stale": self.stale,
            "checkpoints": list(self.checkpoints),
        }


def _iso_timestamp(value) -> str | None:
    """Render a timestamp without exposing the host's local timezone."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


def _short_value(value, limit: int = 900) -> str:
    """Make tool arguments/results bounded and JSON-friendly."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _conversation_events(
    ticks: list, context_ticks: int
) -> tuple[list[dict], dict | None]:
    """Collapse repeated streaming chunks into completed and active turns.

    Explicit Live metadata supplies confirmed fragments, not cumulative text.
    Legacy adapters can repeat cumulative Agent text on silent ticks. User
    chunks remain incremental and are grouped by utterance ID.
    """
    from tau2.utils.transcript.live import live_metadata

    # Reconstruct turns from the full in-memory trajectory. The tick bound
    # must not discard an earlier completed dialogue turn merely because the
    # adapter emitted many silent ticks afterward.
    recent = ticks
    turns: dict[tuple, dict] = {}
    open_keys: dict[str, tuple] = {}
    active_speech: dict[str, bool] = {"user": False, "agent": False}
    order = 0

    for tick in recent:
        tick_time = getattr(tick, "timestamp", None)
        for role, chunk in (("user", tick.user_chunk), ("agent", tick.agent_chunk)):
            if chunk is None:
                continue
            if role == "agent":
                metadata = live_metadata(tick)
                if metadata is not None:
                    for index, fragment in enumerate(metadata.fragments):
                        if fragment.speaker != "agent" or not fragment.text.strip():
                            continue
                        key = (
                            role,
                            "live",
                            metadata.session_id,
                            fragment.event_id or (tick.tick_id, index),
                        )
                        if key not in turns:
                            order += 1
                            turns[key] = {
                                "role": role,
                                "start_time": tick_time,
                                "end_time": tick_time,
                                "time_basis": "asr_release_host_wall_clock",
                                "status": "completed",
                                "is_final": True,
                                "_order": order,
                            }
                        turns[key].update(
                            text=fragment.text.strip(),
                            audio_start_ms=fragment.start_ms,
                            audio_end_ms=fragment.end_ms,
                        )
                    continue
            raw_content = chunk.content or ""
            content = raw_content.strip()
            speaking = bool(chunk.contains_speech)
            identifiers = tuple(chunk.utterance_ids or ())
            key = (role, identifiers) if identifiers else None
            if speaking:
                if key is None:
                    key = open_keys.get(role)
                    if key is None or not active_speech[role]:
                        order += 1
                        key = (role, f"synthetic-{order}")
                elif open_keys.get(role) not in (None, key):
                    previous = turns.get(open_keys[role])
                    if previous is not None and previous["status"] == "speaking":
                        previous["status"] = "interrupted"
                    active_speech[role] = False
                open_keys[role] = key
                active_speech[role] = True
            elif key is None:
                key = open_keys.get(role)
                if key is None and role == "agent" and content:
                    # A realtime adapter may persist the settled transcript on
                    # silent ticks without utterance IDs.  Use the transcript
                    # itself as a stable key so it appears once as history.
                    key = (role, "settled", content)

            # A silent chunk with cumulative Agent text is the settled version
            # of the preceding utterance.  Never create a new turn from it.
            if not content or key is None:
                if (
                    not speaking
                    and key is not None
                    and role == "user"
                    and chunk.is_final_chunk
                ):
                    previous = turns.get(key)
                    if previous is not None and previous["status"] == "speaking":
                        previous["status"] = "interrupted"
                    active_speech[role] = False
                    open_keys.pop(role, None)
                continue

            event = turns.get(key)
            if event is None:
                event = {
                    "role": role,
                    "text": "",
                    "start_time": tick_time,
                    "end_time": tick_time,
                    "status": "speaking" if speaking else "completed",
                    "is_final": bool(chunk.is_final_chunk),
                    "_order": order,
                }
                turns[key] = event
            event["end_time"] = tick_time
            event["is_final"] = bool(chunk.is_final_chunk)
            if role == "user":
                event["text"] += raw_content
            else:
                # Agent realtime output is cumulative across ticks.
                event["text"] = content
            event["status"] = (
                "speaking" if speaking and not chunk.is_final_chunk else "completed"
            )
            if event["status"] == "completed":
                active_speech[role] = False
                if role == "user" and open_keys.get(role) == key:
                    open_keys.pop(role, None)

    events = sorted(turns.values(), key=lambda item: item["_order"])
    active = None
    for event in events:
        event["text"] = event["text"].strip()
        if len(event["text"]) > 3000:
            event["text"] = event["text"][-3000:]
        event.pop("_order", None)
        if event["status"] == "speaking":
            active = event
    if len(events) > 40:
        completed = [event for event in events if event is not active]
        events = completed[-40:]
        if active is not None:
            events.append(active)
    return events, active


def _tool_events(ticks: list, environment_trace: list[dict]) -> list[dict]:
    """Return the latest twenty tool operations with bounded evidence."""
    records: dict[str, dict] = {}
    ordinal = 0
    for row in environment_trace or []:
        if row.get("kind") not in {"tool", "environment"} or not row.get("name"):
            continue
        ordinal += 1
        call_id = row.get("call_id") or f"environment-{ordinal}"
        records[call_id] = {
            "requestor": "agent",
            "tool": row.get("name"),
            "arguments": _short_value(row.get("arguments", {})),
            "start_time": _iso_timestamp(row.get("started_time") or row.get("time")),
            "end_time": _iso_timestamp(row.get("observed_time") or row.get("time")),
            "duration_seconds": None,
            "time_basis": "environment",
            "status": "error" if row.get("error") else "success",
            "result_summary": _short_value(row.get("result")),
        }

    for tick in ticks:
        tick_time = getattr(tick, "timestamp", None)
        for call in getattr(tick, "agent_tool_calls", []) or []:
            call_id = call.id or f"tick-{getattr(tick, 'tick_id', ordinal)}-{call.name}"
            records.setdefault(
                call_id,
                {
                    "requestor": call.requestor,
                    "tool": call.name,
                    "arguments": _short_value(call.arguments),
                    "start_time": tick_time,
                    "end_time": None,
                    "duration_seconds": None,
                    "time_basis": "host_wall_clock",
                    "status": "pending",
                    "result_summary": None,
                },
            )
        for result in getattr(tick, "agent_tool_results", []) or []:
            record = records.setdefault(
                result.id,
                {
                    "requestor": result.requestor,
                    "tool": "unknown",
                    "arguments": "{}",
                    "start_time": None,
                    "end_time": tick_time,
                    "duration_seconds": None,
                    "time_basis": "host_wall_clock",
                    "status": "error" if result.error else "success",
                    "result_summary": _short_value(result.content),
                },
            )
            if record["time_basis"] == "host_wall_clock":
                record["end_time"] = tick_time
            record["status"] = (
                "error"
                if result.error or result.execution_status == "rejected"
                else "success"
            )
            record["result_summary"] = _short_value(result.content)
    return list(records.values())[-20:]


def compact_watchdog_context(
    *,
    task_text: str,
    ticks: list,
    environment_trace: list[dict],
    pending: dict,
    context_ticks: int = 120,
    current_time: float | str | None = None,
    wall_time: float | str | None = None,
    stage: dict | None = None,
) -> str:
    """Build an event-oriented, bounded history view for one watchdog request."""
    conversations, active_turn = _conversation_events(ticks, context_ticks)
    heartbeat_age = pending.get("heartbeat_age_seconds")
    heartbeat_timeout = pending.get("heartbeat_timeout_seconds")
    heartbeat_alive = not pending.get("heartbeat_stale", False) and (
        heartbeat_age is None
        or heartbeat_timeout is None
        or heartbeat_age <= heartbeat_timeout
    )
    payload = {
        "task": task_text[-6000:],
        "environment": {
            "current_time": _iso_timestamp(current_time),
            "current_time_epoch": current_time,
            "host_wall_time": _iso_timestamp(wall_time),
            "stage": stage,
        },
        "heartbeat": {
            "alive": heartbeat_alive,
            "stale": bool(pending.get("heartbeat_stale", False)),
            "last_update_age_seconds": heartbeat_age,
            "timeout_seconds": heartbeat_timeout,
            "phase": pending.get("phase"),
            "phase_age_seconds": pending.get("phase_age_seconds"),
        },
        "conversation": {
            "time_basis": "host_wall_clock",
            "completed_turns": [row for row in conversations if row is not active_turn],
            "active_turn": active_turn,
        },
        "recent_tool_calls": _tool_events(ticks, environment_trace),
        "pending_operations": {
            key: value
            for key, value in pending.items()
            if key
            in {
                "tool_executor_pending",
                "pending_agent_tool_results",
                "pending_user_tool_results",
                "agent_has_pending_output",
                "user_output_pending",
                "user_speech_active",
                "user_speech_final",
                "agent_speech_active",
                "configured_tool_timeout_seconds",
                "scenario_user_input",
                "termination_reason",
            }
        },
    }
    return "WATCHDOG EXECUTION SUMMARY (JSON):\n" + json.dumps(
        payload, ensure_ascii=False, default=str, indent=2
    )

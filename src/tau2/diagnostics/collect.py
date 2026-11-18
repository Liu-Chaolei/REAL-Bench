"""Trial-local observation at existing environment and media boundaries."""

import base64
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tau2.data_model.diagnostics import DiagnosticEvidence, OperationEvidence, UserInput
from tau2.diagnostics.intent.io import digest, write_json, write_jsonl


class EvidenceRecorder:
    """Copy executing state under the environment lock; persist outside it."""

    def __init__(self, environment, task, directory):
        self.environment, self.task, self.directory = environment, task, Path(directory)
        self.operations, self.frames, self.errors = [], {}, []
        self.frame_labels = {}
        self.media_ticks = []
        self.media_times = []
        self.writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tau-evidence"
        )
        self.pending = []
        self.lock = threading.RLock()
        self.closed = False
        self.origin = time.monotonic()
        self.initial_frame = self.capture("initial")

    def now(self):
        runtime = getattr(self.environment, "scenario_runtime", None)
        return runtime.time() if runtime is not None else time.monotonic() - self.origin

    def frame(self, apps=None):
        tools = self.environment.tools
        if hasattr(tools, "apps"):
            from tau2.domains.gaia2.tools import json_value

            states = {
                name: json_value(app.get_state())
                for name, app in tools.apps.items()
                if name != "Files" and (apps is None or name in apps)
            }
            if "Files" in tools.apps and (apps is None or "Files" in apps):
                tools.refresh_db(include_files=True)
                states["Files"] = copy.deepcopy(tools.db.apps["Files"])
            return states
        db = getattr(tools, "db", None)
        return db.model_dump(mode="json") if db is not None else {}

    def capture(self, label, apps=None):
        try:
            value = self.frame(apps)
            if len(json.dumps(value)) > 64 * 1024 * 1024:
                raise ValueError("state_frame_exceeds_64_mib")
            key = digest(value)
            self.frame_labels[label] = key
            with self.lock:
                if key not in self.frames:
                    self.frames[key] = value
                    self.pending.append(
                        self.writer.submit(
                            write_json,
                            self.directory / "state-frames" / (key + ".json"),
                            value,
                        )
                    )
            return key
        except Exception as exc:
            self.errors.append(
                {"kind": "state_capture", "label": label, "error": type(exc).__name__}
            )
            return None

    def before(self, message, arguments):
        fn = self.environment.tools.tools.get(message.name)
        kind = getattr(fn, "__tool_type__", None)
        # Use the original Gaia2 operation metadata, never mutates_state.
        native = getattr(self.environment.tools, "_operations", {}).get(message.name)
        if native is not None:
            kind = "WRITE" if native.write_operation else "READ"
        elif message.name.startswith("System__"):
            kind = "SYSTEM"
        else:
            kind = getattr(kind, "value", kind) or "UNKNOWN"
        kind = str(kind).upper()
        if kind not in {"READ", "WRITE", "SYSTEM"}:
            kind = "UNKNOWN"
        app = message.name.split("__")[0]
        selected = (
            {app, "Files"}
            if any("path" in k or "attachment" in k for k in arguments)
            else {app}
        )
        before = (
            self.capture("before:" + message.id, selected) if kind == "WRITE" else None
        )
        return {
            "started": self.now(),
            "kind": kind,
            "app": app,
            "before": before,
            "apps": selected,
        }

    def after(self, token, message, arguments, result):
        after = (
            self.capture("after:" + message.id, token["apps"])
            if token["kind"] == "WRITE"
            else None
        )
        try:
            content = json.loads(result.content)
        except (TypeError, ValueError):
            content = result.content
        self.operations.append(
            OperationEvidence(
                call_id=message.id,
                name=message.name,
                app=token["app"],
                kind=token["kind"],
                raw_arguments=copy.deepcopy(message.arguments),
                arguments=copy.deepcopy(arguments),
                result=content,
                error=result.error,
                started=token["started"],
                ended=self.now(),
                before_frame=token["before"],
                after_frame=after,
                requestor=message.requestor,
            )
        )

    def media(
        self,
        user_chunk,
        agent_chunk,
        started,
        ended,
        duration,
        input_audio=None,
        input_format=None,
    ):
        """Record exactly the input consumed and output played by the agent tick."""
        from tau2.data_model.message import Tick

        if input_audio is not None:
            if user_chunk is None:
                from tau2.data_model.message import UserMessage

                user_chunk = UserMessage(role="user", contains_speech=False)
            user_chunk = user_chunk.model_copy(
                update={
                    "audio_content": base64.b64encode(input_audio).decode(),
                    "audio_format": input_format,
                }
            )
        self.media_ticks.append(
            Tick(
                tick_id=len(self.media_ticks),
                timestamp=agent_chunk.timestamp,
                tick_duration_seconds=duration,
                user_chunk=copy.deepcopy(user_chunk),
                agent_chunk=copy.deepcopy(agent_chunk),
            )
        )
        self.media_times.append((started, ended))

    def stage(self, label):
        self.capture(label)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.final_frame = self.capture("final")
        self.writer.shutdown(wait=True)
        for pending in self.pending:
            try:
                pending.result()
            except Exception as exc:
                self.errors.append({"kind": "state_write", "error": type(exc).__name__})


def delivered_inputs(simulation, task, index_offset=0):
    """Read delivered content only; partial source scripts are never whole inputs."""
    from tau2.utils.transcript.audio_text import (
        extract_delivered_text,
        merge_audio_script_gold,
    )
    from tau2.utils.transcript.views import utterance_groups, utterance_interrupted

    result = []
    stages = (simulation.info or {}).get("scenario_series", {}).get("stages", [])
    duration = (
        (simulation.ticks[0].tick_duration_seconds or 0.2) if simulation.ticks else 0
    )
    if simulation.ticks:
        for speaker in ("caller", "agent"):
            for group in utterance_groups(simulation.ticks, speaker):
                if not any(c.contains_speech for c in group.chunks):
                    continue
                gold = merge_audio_script_gold(
                    [c.audio_script_gold for c in group.chunks]
                )
                text = (
                    extract_delivered_text(gold)[0]
                    if gold
                    else "".join(
                        c.content or "" for c in group.chunks if c.contains_speech
                    )
                )
                raw = group.chunks[0].raw_data
                live = raw.get("live_segment", {}) if isinstance(raw, dict) else {}
                unavailable = live.get("audio_alignment_error")
                # Actual recorded speech without finalized ASR is missing
                # evidence, not silence. Keep a placeholder so downstream
                # diagnostics cannot quietly ignore the untranscribed reply.
                if not text and speaker == "agent":
                    unavailable = unavailable or "missing_delivered_agent_transcript"
                if (not text and not unavailable) or any(
                    s in text
                    for s in ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")
                ):
                    continue
                start, end = (
                    group.start_index * duration,
                    (group.end_index + 1) * duration,
                )
                stage = None
                for row in stages:
                    # Tick sequence is authoritative for media-stage membership.
                    if (
                        row.get("started")
                        and row["started"]["index"] <= group.start_index + index_offset
                    ):
                        stage = row["stage"]
                interrupted = utterance_interrupted(group.chunks) or (
                    speaker == "caller"
                    and not any(c.is_final_chunk for c in group.chunks)
                )
                result.append(
                    UserInput(
                        id=f"{speaker}:{group.start_tick}",
                        text=text,
                        started=start,
                        ended=end,
                        stage=stage,
                        complete=not interrupted,
                        role="user" if speaker == "caller" else "assistant_context",
                        unavailable_reason=unavailable
                        or (
                            "partial_delivery_text_unverified"
                            if interrupted and speaker == "caller" and not gold
                            else None
                        ),
                    )
                )
    else:
        for index, message in enumerate(simulation.messages or []):
            if (
                message.role not in {"user", "assistant"}
                or message.is_tool_call()
                or not message.content
            ):
                continue
            if any(
                s in message.content
                for s in ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")
            ):
                continue
            result.append(
                UserInput(
                    id=str(index),
                    text=message.content,
                    started=float(index),
                    ended=float(index),
                    role="user" if message.role == "user" else "assistant_context",
                )
            )
    return sorted(result, key=lambda row: (row.started, row.id))


def collect_audio(simulation, directory, orchestrator=None):
    """Persist the actual tick playback, with a monotonic-to-media mapping."""
    if not simulation.ticks:
        return {"status": "not_applicable", "reason": "text_run"}

    rows, channels = [], {"user": bytearray(), "agent": bytearray()}
    position = 0.0
    mapping = (
        getattr(orchestrator.environment, "_diagnostic_media_timeline", [])
        if orchestrator
        else []
    )
    for index, tick in enumerate(simulation.ticks):
        duration = tick.tick_duration_seconds or 0.2
        row = {"tick": tick.tick_id, "start": position, "end": position + duration}
        if tick.tick_id in mapping:
            row["monotonic_start"], row["monotonic_end"] = mapping[tick.tick_id]
        for role, chunk in (("user", tick.user_chunk), ("agent", tick.agent_chunk)):
            if chunk is None or not chunk.audio_content:
                row[role] = None
                continue
            raw = base64.b64decode(chunk.audio_content)
            fmt = chunk.audio_format
            row[role] = {
                "offset": len(channels[role]),
                "bytes": len(raw),
                "format": fmt.model_dump(mode="json"),
                "utterance_ids": chunk.utterance_ids or [],
            }
            channels[role].extend(raw)
        rows.append(row)
        position += duration
    for role, data in channels.items():
        (directory / (role + ".audio")).write_bytes(data)
    write_jsonl(directory / "audio-timeline.jsonl", rows)
    return {
        "status": "collected",
        "duration": position,
        "timeline": rows,
        "files": {r: r + ".audio" for r in channels},
    }


def finalize_evidence(orchestrator, simulation, recorder, directory):
    """Seal evidence before model grading; an incomplete source remains explicit."""
    from tau2.data_model.diagnostics import BackendInput

    directory = Path(directory)
    recorder.close()
    source = getattr(
        orchestrator.agent,
        "get_diagnostic_evidence",
        lambda: {"status": "not_applicable"},
    )()
    inputs = [
        BackendInput.model_validate(row) for row in source.get("backend_inputs", [])
    ]
    media_simulation = simulation
    if recorder.media_ticks:
        media_simulation = simulation.model_copy(update={"ticks": recorder.media_ticks})
        orchestrator.environment._diagnostic_media_timeline = dict(
            enumerate(recorder.media_times)
        )

    evidence = DiagnosticEvidence(
        simulation_id=simulation.id,
        task_id=simulation.task_id,
        domain=orchestrator.environment.get_domain_name(),
        user_inputs=delivered_inputs(
            media_simulation,
            orchestrator.task,
            index_offset=1 if recorder.media_ticks else 0,
        ),
        backend_inputs=inputs,
        backend_status=source.get("status", "not_applicable"),
        backend_reason=source.get("reason"),
        operations=recorder.operations,
        frames=recorder.frames,
        stages=(simulation.info or {}).get("scenario_series", {}),
        source={
            "task_sha256": digest(orchestrator.task.model_dump(mode="json")),
            "metrics_config": getattr(recorder, "metrics_config", None),
            "active_metrics": (simulation.info or {}).get("active_metrics"),
            "final_frame": getattr(recorder, "final_frame", None),
            "initial_frame": recorder.initial_frame,
            "frame_labels": recorder.frame_labels,
            "scenario_monotonic_origin": getattr(
                getattr(orchestrator.environment, "scenario_runtime", None),
                "_origin",
                None,
            ),
            "scenario_start_time": orchestrator.task.scenario.start_time
            if orchestrator.task.scenario
            else None,
            "termination_reason": simulation.termination_reason.value,
        },
        errors=recorder.errors,
        complete=simulation.termination_reason.value
        not in {"infrastructure_error", "user_error", "unexpected_error"},
    )
    try:
        evidence.audio = collect_audio(media_simulation, directory, orchestrator)
    except Exception as exc:
        evidence.audio = {"status": "unavailable", "reason": type(exc).__name__}
    write_jsonl(
        directory / "user-inputs.jsonl",
        [x.model_dump(mode="json") for x in evidence.user_inputs],
    )
    write_jsonl(
        directory / "backend-inputs.jsonl",
        [x.model_dump(mode="json") for x in evidence.backend_inputs],
    )
    write_jsonl(
        directory / "operations.jsonl",
        [x.model_dump(mode="json") for x in evidence.operations],
    )
    write_json(directory / "evidence.json", evidence.model_dump(mode="json"))
    files = {
        p.relative_to(directory).as_posix(): __import__("hashlib")
        .sha256(p.read_bytes())
        .hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    write_json(
        directory / "manifest.json",
        {"version": evidence.version, "files": files, "errors": evidence.errors},
    )
    return evidence

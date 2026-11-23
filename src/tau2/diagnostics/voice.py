"""REAL waveform measurements over tau's actual delivered media timeline."""

from pathlib import Path

from tau2.diagnostics.media_clock import (
    MEDIA_CLOCK_VERSION,
    map_scenario_time_to_media,
)
from tau2.metrics import real_voice
from tau2.metrics.voice_interaction import EVENT_VERSION

VERSION = "tau-real-voice/4"
CONFIG = {
    **real_voice.CONFIG,
    "time_basis": "tau_actual_media_delivery_and_playback",
    "turn_basis": "delivered_user_turns",
    "media_clock_version": MEDIA_CLOCK_VERSION,
}


def map_tool_operations(operations, timeline, scenario_start_time, monotonic_origin):
    """Convert tool evidence one row at a time and retain mapping diagnostics."""

    operations = [
        op for op in operations if op.kind != "SYSTEM" and op.requestor == "assistant"
    ]
    mapped_calls = []
    partial_operations = 0
    unmapped_operations = 0
    unavailable_operations = []
    for op in operations:
        started = map_scenario_time_to_media(
            op.started, timeline, scenario_start_time, monotonic_origin
        )
        ended = map_scenario_time_to_media(
            op.ended, timeline, scenario_start_time, monotonic_origin
        )
        if started["status"] == ended["status"] == "mapped":
            if ended["media_seconds"] < started["media_seconds"]:
                unmapped_operations += 1
                unavailable_operations.append(
                    {"call_id": op.call_id, "reason": "invalid_time_mapping"}
                )
            else:
                mapped_calls.append(
                    {
                        "call_id": op.call_id,
                        "completed": True,
                        "started_mono": started["media_seconds"],
                        "completed_mono": ended["media_seconds"],
                    }
                )
        elif started["status"] == "mapped" or ended["status"] == "mapped":
            partial_operations += 1
            unavailable_operations.append(
                {
                    "call_id": op.call_id,
                    "reason": "partial_time_mapping",
                    "started": started["reason"],
                    "ended": ended["reason"],
                }
            )
        else:
            unmapped_operations += 1
            unavailable_operations.append(
                {
                    "call_id": op.call_id,
                    "reason": started["reason"] or ended["reason"],
                }
            )
    tool_context = {
        "total_operations": len(operations),
        "mapped_operations": len(mapped_calls),
        "partially_mapped_operations": partial_operations,
        "unmapped_operations": unmapped_operations,
        "mapping_version": MEDIA_CLOCK_VERSION,
    }
    if unavailable_operations:
        tool_context["unavailable_operations"] = unavailable_operations
    if not operations:
        calls = []
    elif mapped_calls:
        calls = mapped_calls
    else:
        calls = None
    return calls, tool_context


def evaluate_voice(evidence, directory: Path):
    """Measure waveforms, never substitute contains_speech or caption arrival."""
    if evidence.audio.get("status") != "collected":
        return {
            "status": evidence.audio.get("status", "unavailable"),
            "reason": evidence.audio.get("reason"),
            "version": VERSION,
        }
    shared = evidence.audio.get("activity")
    if shared and shared.get("user_event_version") == EVENT_VERSION:
        from tau2.metrics.voice_interaction import evaluate

        origin = evidence.source.get("scenario_monotonic_origin")
        epoch = evidence.source.get("scenario_start_time")
        calls = None
        if origin is not None and epoch is not None:
            calls = [
                dict(
                    call_id=op.call_id,
                    started_mono=op.started - epoch + origin - shared["origin"],
                )
                for op in evidence.operations
                if op.kind != "SYSTEM" and op.requestor == "assistant"
            ]
        return evaluate(
            shared,
            calls,
            task_key=(
                evidence.domain,
                evidence.source.get("language"),
                evidence.source.get("task_set_name"),
                evidence.task_id,
            ),
        )
    from tau2.data_model.audio import AudioData, AudioFormat
    from tau2.voice.utils.audio_preprocessing import convert_to_pcm16

    timeline = evidence.audio["timeline"]
    raw = {
        role: (directory / evidence.audio["files"][role]).read_bytes()
        for role in ("user", "agent")
    }
    shared = evidence.audio.get("activity")
    if shared is not None:
        turns = [t["intervals"] for t in shared["user_turns"]]
        intervals = {
            "user": [r for t in turns for r in t],
            "agent": shared["agent_intervals"],
        }
    else:
        intervals = {"user": [], "agent": []}
        for role in intervals:
            # Detect on the complete channel, not separately on 200 ms frames.
            chunks, formats, maps, count = [], set(), [], 0
            for row in timeline:
                item = row.get(role)
                if not item:
                    continue
                fmt = AudioFormat.model_validate(item["format"])
                formats.add(fmt.sample_rate)
                pcm = convert_to_pcm16(
                    AudioData(
                        data=raw[role][item["offset"] : item["offset"] + item["bytes"]],
                        format=fmt,
                    )
                ).data
                n = len(pcm) // 2
                maps.append(
                    {
                        "sample_start": count,
                        "sample_end": count + n,
                        "mono_start": row["start"],
                        "mono_end": row["end"],
                    }
                )
                chunks.append(pcm)
                count += n
            if len(formats) != 1:
                return {
                    "status": "unavailable",
                    "reason": "missing_or_changing_audio_format",
                    "version": VERSION,
                }
            rate = next(iter(formats))
            voiced = (
                real_voice.detect_speech(b"".join(chunks), rate)
                if role == "agent"
                else [
                    [row.started, row.ended]
                    for row in evidence.user_inputs
                    if row.role == "user"
                ]
            )
            if role == "user":
                intervals[role] = voiced
                continue
            # Map each sample interval into delivered time, preserving channel gaps.
            for a, b in voiced:
                for row in maps:
                    left, right = (
                        max(a * rate, row["sample_start"]),
                        min(b * rate, row["sample_end"]),
                    )
                    if right > left:
                        scale = (row["mono_end"] - row["mono_start"]) / (
                            row["sample_end"] - row["sample_start"]
                        )
                        intervals[role].append(
                            [
                                row["mono_start"]
                                + (left - row["sample_start"]) * scale,
                                row["mono_start"]
                                + (right - row["sample_start"]) * scale,
                            ]
                        )
        turns = []
        for row in evidence.user_inputs:
            if row.role != "user":
                continue
            pieces = [
                [max(a, row.started), min(b, row.ended)]
                for a, b in intervals["user"]
                if min(b, row.ended) > max(a, row.started)
            ]
            if pieces:
                turns.append(real_voice._intervals(pieces))
    waveform = {
        "version": real_voice.EVIDENCE_VERSION,
        "config": real_voice.CONFIG,
        "status": "valid" if turns else "invalid",
        "reason": None if turns else "no_user_speech",
        "user_intervals": [r for t in turns for r in t],
        "user_turns": turns,
        "agent_intervals": intervals["agent"],
        "agent_segments": real_voice._intervals(
            intervals["agent"], real_voice.CONFIG["merge_gap_sec"]
        ),
        "input_start_mono": 0.0,
        "cutoff_mono": evidence.audio["duration"],
    }
    # Operations use scenario seconds. Map each operation independently through
    # the recorded monotonic/media timeline so one gap cannot erase all context.
    origin = evidence.source.get("scenario_monotonic_origin")
    epoch = evidence.source.get("scenario_start_time")
    if shared is not None:
        calls = [] if origin is not None and epoch is not None else None
        if calls is not None:
            calls = [
                dict(
                    call_id=op.call_id,
                    completed=True,
                    started_mono=op.started - epoch + origin - shared["origin"],
                    completed_mono=op.ended - epoch + origin - shared["origin"],
                )
                for op in evidence.operations
                if op.kind != "SYSTEM" and op.requestor == "assistant"
            ]
        tool_context = {
            "total_operations": len(evidence.operations),
            "time_basis": "direct_media_events",
        }
    else:
        calls, tool_context = map_tool_operations(
            evidence.operations, timeline, epoch, origin
        )
    result = real_voice.score_interactions(waveform, calls)
    if calls is None and tool_context["total_operations"]:
        for name in ("on_time_rate",):
            result["metrics"][name]["reason"] = "tool_media_time_mapping_unavailable"
    result.setdefault("details", {})["tool_context"] = tool_context
    result.update(
        version=VERSION,
        status="measured",
        namespace="real_voice",
        waveform_evidence=waveform,
        source_metric_version=real_voice.VERSION,
        config={**CONFIG, **(shared["config"] if shared else {})},
    )
    return result


def aggregate_voice(reports):
    """Use REAL pooling formulas while enforcing tau's own media-time contract."""
    from tau2.metrics.voice_interaction import VERSION as unified_version
    from tau2.metrics.voice_interaction import aggregate

    if not any(reports) or any(
        r and r.get("version") == unified_version for r in reports
    ):
        return aggregate(reports)
    if all(not r or r.get("version") == "tau-voice-interaction/3" for r in reports):
        # Preserve saved values and sample weights, with their original definition.
        old = next(r for r in reports if r)
        result = aggregate(
            [dict(r, version=unified_version) if r else None for r in reports]
        )
        return dict(result, version=old["version"], config=old["config"])
    for report in reports:
        if report and (
            report.get("version") != VERSION or report.get("config") != CONFIG
        ):
            raise ValueError(
                "Cannot pool different voice versions or time/turn definitions"
            )
    native = [
        dict(r, version=real_voice.VERSION, config=real_voice.CONFIG) if r else None
        for r in reports
    ]
    result = real_voice.aggregate_interactions(native)
    return dict(
        result, version=VERSION, config=CONFIG, source_metric_version=real_voice.VERSION
    )

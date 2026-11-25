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
    from tau2.diagnostics.media import read_media_activity

    media = read_media_activity(evidence, directory)
    if media["status"] != "measured":
        return {**media, "version": VERSION}
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
    timeline = media["timeline"]
    turns = media["user_turns"]
    waveform = {
        "version": real_voice.EVIDENCE_VERSION,
        "config": real_voice.CONFIG,
        "status": "valid" if turns else "invalid",
        "reason": None if turns else "no_user_speech",
        "user_intervals": [r for t in turns for r in t],
        "user_turns": turns,
        "agent_intervals": media["agent_intervals"],
        "agent_segments": real_voice._intervals(
            media["agent_intervals"], real_voice.CONFIG["merge_gap_sec"]
        ),
        "input_start_mono": 0.0,
        "cutoff_mono": media["duration"],
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
        config={**CONFIG, **(shared.get("config", {}) if shared else {})},
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

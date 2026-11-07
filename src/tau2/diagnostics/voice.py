"""REAL waveform measurements over tau's actual delivered media timeline."""

from pathlib import Path

from tau2.metrics import real_voice

VERSION = "tau-real-voice/1"
CONFIG = {
    **real_voice.CONFIG,
    "time_basis": "tau_actual_media_delivery_and_playback",
    "turn_basis": "delivered_user_turns",
}


def evaluate_voice(evidence, directory: Path):
    """Measure waveforms, never substitute contains_speech or caption arrival."""
    if evidence.audio.get("status") != "collected":
        return {
            "status": evidence.audio.get("status", "unavailable"),
            "reason": evidence.audio.get("reason"),
            "version": VERSION,
        }
    from tau2.data_model.audio import AudioData, AudioFormat
    from tau2.voice.utils.audio_preprocessing import convert_to_pcm16

    timeline = evidence.audio["timeline"]
    raw = {
        role: (directory / evidence.audio["files"][role]).read_bytes()
        for role in ("user", "agent")
    }
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
        voiced = real_voice.detect_speech(b"".join(chunks), rate)
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
                            row["mono_start"] + (left - row["sample_start"]) * scale,
                            row["mono_start"] + (right - row["sample_start"]) * scale,
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
    # Operations use scenario seconds. Interpolate through recorded monotonic
    # samples only when their scenario origin is explicitly known.
    origin = evidence.source.get("scenario_monotonic_origin")
    epoch = evidence.source.get("scenario_start_time")

    def media_time(value):
        target = value - epoch + origin
        for row in timeline:
            a, b = row.get("monotonic_start"), row.get("monotonic_end")
            if a is not None and b is not None and a <= target <= b and b > a:
                return row["start"] + (target - a) / (b - a) * (
                    row["end"] - row["start"]
                )
        raise ValueError("tool_media_time_mapping_unavailable")

    calls = None
    if not evidence.operations:
        calls = []
    elif origin is not None and epoch is not None:
        try:
            calls = [
                {
                    "call_id": op.call_id,
                    "completed": True,
                    "started_mono": media_time(op.started),
                    "completed_mono": media_time(op.ended),
                }
                for op in evidence.operations
                if op.kind != "SYSTEM" and op.requestor == "assistant"
            ]
        except ValueError:
            calls = None
    result = real_voice.score_interactions(waveform, calls)
    result.update(
        version=VERSION,
        status="measured",
        namespace="real_voice",
        waveform_evidence=waveform,
        source_metric_version=real_voice.VERSION,
        config=CONFIG,
    )
    return result


def aggregate_voice(reports):
    """Use REAL pooling formulas while enforcing tau's own media-time contract."""
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

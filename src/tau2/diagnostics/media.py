"""Public media activity facts shared by diagnostics, independent of scores."""

from pathlib import Path

from tau2.voice.audio_activity import merge_intervals

MEDIA_ACTIVITY_VERSION = "tau-media-activity/1"


def read_media_activity(evidence, directory: Path):
    """Return observed speech intervals on the relative media clock.

    Current activity is read directly. Historical audio uses the existing VAD,
    without invoking voice scoring or requiring classified user events.
    """
    if evidence.audio.get("status") != "collected":
        return {
            "status": "unavailable",
            "reason": evidence.audio.get("reason") or "missing_audio",
        }
    shared = evidence.audio.get("activity")
    common = {
        "version": MEDIA_ACTIVITY_VERSION,
        "time_basis": "media_relative_seconds",
        "duration": evidence.audio["duration"],
        "timeline": evidence.audio.get("timeline", []),
    }
    if shared is not None:
        if shared.get("agent_intervals") is None:
            return {
                **common,
                "status": "unavailable",
                "reason": "missing_agent_activity",
            }
        return {
            **common,
            "status": "measured",
            "agent_intervals": merge_intervals(shared["agent_intervals"]),
            "user_turns": [t["intervals"] for t in shared.get("user_turns", [])],
            "activity": shared,
        }
    from tau2.data_model.audio import AudioData, AudioFormat
    from tau2.voice.audio_activity import detect_speech
    from tau2.voice.utils.audio_preprocessing import convert_to_pcm16

    timeline = evidence.audio["timeline"]
    raw = {
        role: (directory / evidence.audio["files"][role]).read_bytes()
        for role in ("user", "agent")
        if any(row.get(role) for row in timeline)
    }

    intervals = {"user": [], "agent": []}
    for role in intervals:
        if role not in raw:
            if role == "agent":
                return {"status": "unavailable", "reason": "missing_agent_audio"}
            continue
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
                "version": MEDIA_ACTIVITY_VERSION,
            }
        rate = next(iter(formats))
        voiced = (
            detect_speech(b"".join(chunks), rate)
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
            turns.append(merge_intervals(pieces))
    return {
        **common,
        "status": "measured",
        "agent_intervals": merge_intervals(intervals["agent"]),
        "user_turns": turns,
        "activity": None,
    }

"""Offline, evaluator-owned voice measurements; no provider or judge calls.

Definitions borrow from tau-Voice and EVA, with explicit blackbox adaptations:
overlap averages include zero-overlap requests, and tool lead-ins must be audible.
The waveform detector is imported lazily so scoring/reporting needs no audio SDK.
"""

from __future__ import annotations

import hashlib
import math
import statistics
import wave
from collections import Counter
from pathlib import Path
from typing import Any, TypeGuard

VERSION = "gaia2-voice-interaction/1"
EVIDENCE_VERSION = "gaia2-voice-interaction-evidence/1"
CONFIG: dict[str, Any] = {
    "vad": "webrtcvad-wheels==2.0.14.post1",
    "resampler": "soxr==0.5.0.post1",
    "vad_mode": 2,
    "sample_rate": 16000,
    "frame_ms": 20,
    "merge_gap_sec": 0.2,
    "early_threshold_sec": 0.2,
    "late_threshold_sec": 2.75,
    "late_threshold_tool_sec": 4.0,
    "clock_tolerance_sec": 0.1,
    "response_basis": "silent_at_user_end_then_first_audible_start",
    "overlap_basis": "all_valid_user_requests_including_zero_overlap",
    "tool_lead_in_basis": "audible_speech_before_application_tool_group",
    "percentile": "linear_interpolation",
    "time_basis": "client_input_presentation_and_simulated_playback_monotonic",
}
METRIC_UNITS = {
    "response_latency_mean": "seconds",
    "response_latency_p90": "seconds",
    "response_rate": "fraction",
    "agent_interruption_rate": "fraction",
    "speech_overlap_duration": "seconds",
    "on_time_rate": "fraction",
    "late_rate": "fraction",
    "audible_pretoolspeech_rate": "fraction",
}


def _number(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _intervals(rows: list[list[float]], gap: float = 0.0) -> list[list[float]]:
    """Union intervals; a positive gap also merges short intra-speech pauses."""
    result: list[list[float]] = []
    for start, end in sorted(rows):
        if not _number(start) or not _number(end) or end <= start:
            raise ValueError("invalid_speech_interval")
        if result and (start <= result[-1][1] or start - result[-1][1] < gap):
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def percentile(samples: list[float], p: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def detect_speech(pcm: bytes, rate: int) -> list[list[float]]:
    """Return raw voiced intervals in source seconds, excluding final frame pad."""
    import numpy as np
    import soxr
    import webrtcvad

    if rate <= 0 or len(pcm) % 2:
        raise ValueError("invalid_pcm16")
    if not pcm:
        return []
    target_rate = CONFIG["sample_rate"]
    if rate != target_rate:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        resampled = soxr.resample(samples, rate, target_rate, quality="HQ")
        frames = np.clip(np.rint(resampled), -32768, 32767).astype("<i2").tobytes()
    else:
        frames = pcm
    size = target_rate * CONFIG["frame_ms"] // 1000 * 2
    detector = webrtcvad.Vad(CONFIG["vad_mode"])
    duration = len(pcm) / (rate * 2)
    voiced = []
    for offset in range(0, len(frames), size):
        frame = frames[offset : offset + size]
        if detector.is_speech(frame.ljust(size, b"\0"), target_rate):
            start = offset / (target_rate * 2)
            end = min(duration, (offset + size) / (target_rate * 2))
            if end > start:
                voiced.append([start, end])
    return _intervals(voiced)


def _read_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getcomptype() != "NONE"
        ):
            raise ValueError("requires_mono_pcm16")
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != wav.getnframes() * 2:
            raise ValueError("truncated_wav")
        return pcm, wav.getframerate()


def map_speech(
    voiced: list[list[float]], timeline: list[dict[str, Any]], rate: int, samples: int
) -> list[list[float]]:
    """Map sample offsets through actual per-packet/per-playback time receipts."""
    if samples and not timeline:
        raise ValueError("missing_sample_timeline")
    expected = 0
    mapped = []
    for index, row in enumerate(timeline):
        offset, count = row.get("sample_offset"), row.get("sample_count")
        start, end = row.get("start_mono"), row.get("end_mono")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset != expected
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not _number(start)
            or not _number(end)
            or end <= start
        ):
            raise ValueError("invalid_sample_timeline")
        if index + 1 < len(timeline):
            next_start = timeline[index + 1].get("start_mono")
            if not _number(next_start) or next_start < start:
                raise ValueError("nonmonotonic_sample_timeline")
            if end - next_start > CONFIG["clock_tolerance_sec"]:
                raise ValueError("overlapping_sample_timeline")
            # Input packets are scheduled every 20ms; small scheduling jitter
            # must not manufacture overlaps between successive user samples.
            end = min(end, next_start)
            if end <= start:
                raise ValueError("nonmonotonic_sample_timeline")
        expected += count
        source_start, source_end = offset / rate, (offset + count) / rate
        for a, b in voiced:
            left, right = max(a, source_start), min(b, source_end)
            if right > left:
                mapped.append(
                    [
                        start
                        + (left - source_start)
                        / (source_end - source_start)
                        * (end - start),
                        start
                        + (right - source_start)
                        / (source_end - source_start)
                        * (end - start),
                    ]
                )
    if expected != samples:
        raise ValueError("sample_timeline_count_mismatch")
    return _intervals(mapped)


def analyze_audio(
    input_path: Path, output: Path, observation: dict[str, Any]
) -> dict[str, Any]:
    """Build independently auditable speech evidence after the window is sealed."""
    evidence: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "config": dict(CONFIG),
        "status": "invalid",
        "reason": None,
        "user_intervals": [],
        "agent_intervals": [],
        "agent_segments": [],
        "responses": [],
        "excluded_startup_responses": 0,
        "cutoff_mono": observation.get("cutoff_mono"),
        "input_start_mono": observation.get("input_start_mono"),
    }
    try:
        if observation.get("error") or not observation.get("input_complete"):
            raise ValueError("input_or_transport_incomplete")
        start, cutoff = evidence["input_start_mono"], evidence["cutoff_mono"]
        if not _number(start) or not _number(cutoff) or cutoff <= start:
            raise ValueError("missing_observation_timeline")
        evidence["input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
        turns = []
        inputs = observation.get("user_inputs")
        if inputs is not None:
            if not inputs:
                raise ValueError("missing_user_inputs")
            for item in inputs:
                name = item["audio_file"]
                if Path(name).name != name or not item.get("complete"):
                    raise ValueError("invalid_user_input")
                path = output / name
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    != item["audio_sha256"]
                ):
                    raise ValueError("user_audio_hash_mismatch")
                pcm, rate = _read_pcm(path)
                intervals = map_speech(
                    detect_speech(pcm, rate), item["timeline"], rate, len(pcm) // 2
                )
                if not intervals:
                    raise ValueError("no_user_speech")
                turns.append(intervals)
            if any(left[-1][1] > right[0][0] for left, right in zip(turns, turns[1:])):
                raise ValueError("overlapping_user_inputs")
            evidence["user_turns"] = turns
        else:
            pcm, rate = _read_pcm(input_path)
            turns.append(
                map_speech(
                    detect_speech(pcm, rate),
                    observation.get("input_timeline", []),
                    rate,
                    len(pcm) // 2,
                )
            )
        user = [interval for turn in turns for interval in turn]
        evidence["user_intervals"] = user
        if not user:
            raise ValueError("no_user_speech")
        if user[0][0] < start or user[-1][1] > cutoff:
            raise ValueError("input_speech_outside_window")
        identities: set[str] = set()
        for row in observation.get("responses", []):
            identity = row["response_id"]
            if identity in identities:
                raise ValueError("duplicate_response_evidence")
            identities.add(identity)
            if row.get("phase") != "task":
                evidence["excluded_startup_responses"] += 1
                continue
            if row.get("boundary_error") in {"audio_after_end"}:
                raise ValueError("output_audio_boundary_invalid")
            filename = row["audio_file"]
            if Path(filename).name != filename:
                raise ValueError("invalid_audio_filename")
            path = output / filename
            if hashlib.sha256(path.read_bytes()).hexdigest() != row.get("audio_sha256"):
                raise ValueError("output_audio_hash_mismatch")
            played, output_rate = _read_pcm(path)
            if len(played) // 2 != row.get("played_samples") or output_rate != row.get(
                "sample_rate"
            ):
                raise ValueError("output_audio_sample_mismatch")
            intervals = map_speech(
                detect_speech(played, output_rate),
                row.get("playback_timeline", []),
                output_rate,
                len(played) // 2,
            )
            intervals = [
                [max(start, a), min(cutoff, b)]
                for a, b in intervals
                if min(cutoff, b) > max(start, a)
            ]
            segments = _intervals(intervals, CONFIG["merge_gap_sec"])
            evidence["responses"].append(
                {
                    "response_id": identity,
                    "audio_file": filename,
                    "audio_sha256": row["audio_sha256"],
                    "intervals": intervals,
                    "segments": segments,
                }
            )
            evidence["agent_intervals"].extend(intervals)
            evidence["agent_segments"].extend(segments)
        evidence["agent_intervals"] = _intervals(evidence["agent_intervals"])
        evidence["agent_segments"] = _intervals(
            evidence["agent_segments"], CONFIG["merge_gap_sec"]
        )
        evidence["status"] = "valid"
    except Exception as exc:
        # A missing VAD dependency or damaged WAV is a metric failure, not an
        # alteration of the existing official task verdict.
        evidence["reason"] = (
            str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        )
    return evidence


def _metric(
    name: str,
    value: float | None = None,
    *,
    samples: list[float] | None = None,
    numerator: int | None = None,
    denominator: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "value": value,
        "unit": METRIC_UNITS[name],
        "sample_count": denominator if denominator is not None else len(samples or []),
        "status": "measured" if value is not None else "unavailable",
        "reason": reason,
    }
    if samples is not None:
        result["samples"] = samples
    if denominator is not None:
        result.update(numerator=numerator, denominator=denominator)
    return result


def _overlap(left: list[list[float]], right: list[list[float]]) -> float:
    return sum(max(0.0, min(b, d) - max(a, c)) for a, b in left for c, d in right)


def _tool_groups(
    calls: list[dict[str, Any]], speech: list[list[float]], start: float, cutoff: float
) -> list[dict[str, Any]]:
    ordered = sorted(calls, key=lambda row: row.get("started_mono", -math.inf))
    groups: list[dict[str, Any]] = []
    previous_end = start
    for row in ordered:
        began, ended = row.get("started_mono"), row.get("completed_mono")
        if (
            not _number(began)
            or not _number(ended)
            or not start <= began <= ended <= cutoff
        ):
            raise ValueError("invalid_tool_timeline")
        if not row.get("completed"):
            raise ValueError("unfinished_tool_call")
        spoken = _overlap(speech, [[previous_end, began]]) > 0
        if not groups or spoken:
            groups.append(
                {
                    "start_mono": began,
                    "end_mono": ended,
                    "has_audible_lead_in": spoken,
                    "tool_count": 1,
                }
            )
        else:
            groups[-1]["end_mono"] = max(groups[-1]["end_mono"], ended)
            groups[-1]["tool_count"] += 1
        previous_end = max(previous_end, ended)
    return groups


def score_interactions(
    evidence: dict[str, Any] | None, calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": VERSION,
        "config": dict(CONFIG),
        "metrics": {},
        "details": {},
    }
    reason = "missing_interaction_evidence"
    if evidence:
        reason = evidence.get("reason") or "invalid_interaction_evidence"
    if (
        not evidence
        or evidence.get("version") != EVIDENCE_VERSION
        or evidence.get("config") != CONFIG
        or evidence.get("status") != "valid"
    ):
        report["metrics"] = {
            name: _metric(name, reason=reason) for name in METRIC_UNITS
        }
        return report
    user = _intervals(evidence["user_intervals"])
    agent = _intervals(evidence["agent_intervals"])
    segments = _intervals(evidence["agent_segments"])
    start, cutoff = evidence["input_start_mono"], evidence["cutoff_mono"]
    if (
        not user
        or not _number(start)
        or not _number(cutoff)
        or not start <= user[0][0] < user[-1][1] <= cutoff
    ):
        raise ValueError("invalid_interaction_window")
    turns = evidence.get("user_turns") or [user]
    latencies, overlaps, thresholds = [], [], []
    eligible = interrupted = excluded = 0
    for index, turn in enumerate(turns):
        user_end = turn[-1][1]
        end = turns[index + 1][0][0] if index + 1 < len(turns) else cutoff
        speaking_at_end = any(a < user_end < b for a, b in agent)
        excluded += int(speaking_at_end)
        eligible += int(not speaking_at_end)
        response_starts = [a for a, _ in segments if user_end <= a < end]
        if response_starts and not speaking_at_end:
            latencies.append(response_starts[0] - user_end)
            turn_has_tools = (
                any(
                    turn[0][0] <= c.get("started_mono", -1) < end for c in (calls or [])
                )
                if len(turns) > 1
                else bool(calls)
            )
            thresholds.append(
                CONFIG["late_threshold_tool_sec"]
                if turn_has_tools
                else CONFIG["late_threshold_sec"]
            )
        interrupted += int(
            any(any(a <= began < b for a, b in turn) for began, _ in segments)
        )
        overlaps.append(_overlap(turn, agent))
    responded = len(latencies)
    metrics = report["metrics"]
    latency_reason = "agent_speaking_at_user_end" if not eligible else "no_response"
    metrics["response_latency_mean"] = _metric(
        "response_latency_mean",
        statistics.mean(latencies) if latencies else None,
        samples=latencies,
        reason=None if latencies else latency_reason,
    )
    metrics["response_latency_p90"] = _metric(
        "response_latency_p90",
        percentile(latencies, 0.9),
        samples=latencies,
        reason=None if latencies else latency_reason,
    )
    metrics["response_rate"] = _metric(
        "response_rate",
        responded / eligible if eligible else None,
        numerator=responded,
        denominator=eligible,
        reason=None if eligible else latency_reason,
    )
    metrics["agent_interruption_rate"] = _metric(
        "agent_interruption_rate",
        interrupted / len(turns),
        numerator=interrupted,
        denominator=len(turns),
    )
    metrics["speech_overlap_duration"] = _metric(
        "speech_overlap_duration", statistics.mean(overlaps), samples=overlaps
    )
    tool_reason = "missing_tool_context" if calls is None else latency_reason
    has_tools = bool(calls)
    late_threshold = (
        CONFIG["late_threshold_tool_sec"] if has_tools else CONFIG["late_threshold_sec"]
    )
    for name in ("on_time_rate", "late_rate"):
        count = sum(
            CONFIG["early_threshold_sec"] <= value < threshold
            if name == "on_time_rate"
            else value >= threshold
            for value, threshold in zip(latencies, thresholds)
        )
        n = len(latencies) if calls is not None else 0
        metrics[name] = _metric(
            name,
            count / n if n else None,
            numerator=count if n else 0,
            denominator=n,
            reason=None if n else tool_reason,
        )
    groups = []
    try:
        if calls is None:
            raise ValueError("missing_tool_context")
        groups = _tool_groups(calls, agent, start, cutoff)
        count = sum(group["has_audible_lead_in"] for group in groups)
        n = len(groups)
        metrics["audible_pretoolspeech_rate"] = _metric(
            "audible_pretoolspeech_rate",
            count / n if n else None,
            numerator=count,
            denominator=n,
            reason=None if n else "no_tool_calls",
        )
    except (ValueError, TypeError) as exc:
        metrics["audible_pretoolspeech_rate"] = _metric(
            "audible_pretoolspeech_rate",
            reason=str(exc) if isinstance(exc, ValueError) else "invalid_tool_timeline",
        )
    report["details"] = {
        "user_turns": len(turns),
        "response_eligible_turns": eligible,
        "excluded_response_turns": excluded,
        "excluded_startup_responses": evidence.get("excluded_startup_responses", 0),
        "has_tool_calls": has_tools if calls is not None else None,
        "late_threshold_sec": late_threshold if calls is not None else None,
        "early_responses": sum(
            value < CONFIG["early_threshold_sec"] for value in latencies
        ),
        "tool_groups": groups,
    }
    return report


def aggregate_interactions(reports: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Pool measurements, never average trial percentiles or trial fractions."""
    result: dict[str, Any] = {
        "version": VERSION,
        "config": dict(CONFIG),
        "metrics": {},
        "total_trials": len(reports),
    }
    for report in reports:
        if report and (
            report.get("version") != VERSION or report.get("config") != CONFIG
        ):
            raise ValueError(
                "Do not combine different interaction metric versions/configurations"
            )
    for name in METRIC_UNITS:
        rows = [(r or {}).get("metrics", {}).get(name) for r in reports]
        valid = [row for row in rows if row and row.get("status") == "measured"]
        reasons = Counter(
            (row or {}).get("reason") or "missing_interaction_metrics"
            for row in rows
            if not row or row.get("status") != "measured"
        )
        if METRIC_UNITS[name] == "seconds":
            samples = [value for row in valid for value in row["samples"]]
            value = (
                percentile(samples, 0.9)
                if name == "response_latency_p90"
                else statistics.mean(samples)
                if samples
                else None
            )
            item = _metric(
                name,
                value,
                samples=samples,
                reason=None if samples else "no_valid_samples",
            )
        else:
            numerator = sum(row["numerator"] for row in valid)
            denominator = sum(row["denominator"] for row in valid)
            item = _metric(
                name,
                numerator / denominator if denominator else None,
                numerator=numerator,
                denominator=denominator,
                reason=None if denominator else "no_valid_samples",
            )
        item.update(
            measured_trials=len(valid),
            unavailable_trials=len(rows) - len(valid),
            reasons=dict(reasons),
        )
        result["metrics"][name] = item
    result["excluded_response_turns"] = sum(
        (r or {}).get("details", {}).get("excluded_response_turns", 0) for r in reports
    )
    return result

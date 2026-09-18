"""Chronological playback evidence; filenames never define response order."""

from __future__ import annotations

import json
import math
import wave


def build_timeline(root, *, include_interruptions=False):
    events = [json.loads(s) for s in (root / "gateway.jsonl").read_text().splitlines()]
    protocol = [
        json.loads(s) for s in (root / "protocol.jsonl").read_text().splitlines()
    ]
    start = next(r["wall_time"] for r in protocol if r["event"] == "input.started")
    rows = []
    seen = set()
    for path in root.glob("response-*.json"):
        meta = json.loads(path.read_text())
        identity = meta["response_id"]
        if identity in seen:
            raise ValueError("Duplicate response in playback timeline")
        seen.add(identity)
        starts = [
            r["wall_time"]
            for r in events
            if r.get("type") == "playback.started" and r.get("responseId") == identity
        ]
        cancels = [
            r["wall_time"]
            for r in events
            if r.get("type") == "playback.cancelled" and r.get("responseId") == identity
        ]
        with wave.open(str(path.with_suffix(".wav"))) as audio:
            rate, frames = audio.getframerate(), audio.getnframes()
        if frames != meta["played_samples"] or (frames and len(starts) != 1):
            raise ValueError("Playback timeline does not match recorded audio")
        first = starts[0] - start if starts else None
        cancelled = min(cancels) - start if cancels else None
        ends = [
            r["wall_time"]
            for r in events
            if r.get("type") == "playback.ended" and r.get("responseId") == identity
        ]
        end = min(ends) - start if ends else None
        if any(
            v is not None and not math.isfinite(v) for v in [first, cancelled, end]
        ) or (first is not None and cancelled is not None and cancelled < first):
            raise ValueError("Invalid playback times")
        rows.append(
            {
                "response_id": identity,
                "metadata": path.name,
                "playback_start_sec": first,
                "playback_cancel_sec": cancelled,
                "playback_end_sec": end,
                "task_clock_playback_end_sec": meta.get("playback_end_sec"),
                "played_samples": frames,
                "sample_rate": rate,
                "played_audio_sec": frames / rate,
                "cancelled": meta["cancelled"],
                "heard_transcript_known": meta.get("heard_transcript") is not None,
            }
        )
    rows.sort(
        key=lambda r: (
            r["playback_start_sec"]
            if r["playback_start_sec"] is not None
            else float("inf"),
            r["response_id"],
        )
    )
    result = {
        "schema": "gaia2-playback-timeline/1",
        "order": "playback_start",
        "clock": "wall_seconds_since_input_started",
        "responses": rows,
    }

    if include_interruptions:
        if __package__:
            from .interruption_evidence import build_interruption_evidence
        else:
            from interruption_evidence import build_interruption_evidence
        evidence = build_interruption_evidence(root)
        result["interruption_evidence"] = evidence
    return result

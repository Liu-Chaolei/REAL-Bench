"""Deterministic v4 observation and lossless audible projection (no model calls)."""

from __future__ import annotations

import hashlib
import json
import math
import wave
from pathlib import Path

VERSION = "gaia2-qwen-audio/4"
POLICY = {
    "name": "fixed_observation_window",
    "idle_seconds": 15,
    "maximum_seconds": 2400,
    "boundary_model_calls": 0,
    "partial_audio": "require_verified_alignment_or_incomplete",
    "scope": "one user input; no ENV/CONDITION events; at most one oracle AUI answer",
}


def eligibility(scenario):
    events = scenario.get("events", [])
    reasons = []
    if sum(e.get("event_type") == "USER" for e in events) != 1:
        reasons.append("requires_exactly_one_user_input")
    if any(e.get("event_type") in {"ENV", "CONDITION"} for e in events):
        reasons.append("dynamic_environment_not_supported")
    if (
        sum(
            e.get("event_type") == "AGENT"
            and e.get("action", {}).get("app") == "AgentUserInterface"
            and e.get("action", {}).get("function") == "send_message_to_user"
            for e in events
        )
        > 1
    ):
        reasons.append("multiple_official_answer_boundaries")
    return {"supported": not reasons, "reasons": reasons}


class IdleWindow:
    def __init__(self, seconds=15):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Idle window must be positive and finite")
        self.seconds = seconds
        self.since = None

    def update(self, now, *, idle, activity):
        if not idle:
            self.since = None
            self.activity = activity
            return False
        if self.since is None or self.activity != activity:
            self.since = now
            self.activity = activity
        return now - self.since >= self.seconds

    def reset(self):
        self.since = None


def project_audio(root: Path):
    """Use only fully played, attributable audio; never estimate text prefixes."""
    segments, gaps = [], []
    seen = set()
    for audio in root.glob("response-*.wav"):
        if (
            not audio.name.endswith(".generated.wav")
            and not audio.with_suffix(".json").exists()
        ):
            gaps.append({"file": audio.name, "reason": "missing_playback_metadata"})
    for path in sorted(root.glob("response-*.json")):
        row = json.loads(path.read_text())
        samples = row.get("played_samples", 0)
        if not isinstance(samples, int) or samples < 0:
            gaps.append({"file": path.name, "reason": "invalid_played_samples"})
            continue
        if not samples:
            continue
        identity = row.get("response_id")
        if not identity or identity in seen:
            gaps.append(
                {"file": path.name, "reason": "duplicate_or_missing_response_id"}
            )
            continue
        seen.add(identity)
        audio = path.with_suffix(".wav")
        try:
            with wave.open(str(audio)) as recording:
                if recording.getnframes() != samples:
                    raise ValueError("Played sample count differs")
                if len(recording.readframes(samples)) != samples * 2:
                    raise ValueError("Truncated played audio")
                if (
                    row.get("played_completely")
                    and row.get("received_samples", samples) != samples
                ):
                    raise ValueError(
                        "Complete playback differs from received sample count"
                    )
                if recording.getsampwidth() != 2 or recording.getnchannels() != 1:
                    raise ValueError("Unsupported played audio format")
        except (OSError, EOFError, wave.Error, ValueError):
            gaps.append({"file": path.name, "reason": "played_audio_mismatch"})
            continue
        text = row.get("heard_transcript")
        end = row.get("playback_end_sec")
        if (
            not row.get("played_completely")
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(end, (float, int))
            or not math.isfinite(end)
        ):
            gaps.append({"file": path.name, "reason": "audible_transcript_incomplete"})
            continue
        segments.append(
            {
                "response_id": identity,
                "metadata": path.name,
                "audio": audio.name,
                "played_samples": samples,
                "playback_end_sec": end,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "declared_kind": row.get("declared_kind"),
            }
        )
    segments.sort(key=lambda row: (row["playback_end_sec"], row["response_id"]))
    cursor = 0
    for i, row in enumerate(segments):
        cursor += 2 if i else 0
        row["text_start"] = cursor
        cursor += len(row["text"])
        row["text_end"] = cursor
    text = "\n\n".join(row["text"] for row in segments)
    return {
        "protocol": VERSION,
        "audible_transcript_complete": not gaps,
        "segments": segments,
        "gaps": gaps,
        "text": text if not gaps else None,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest() if not gaps else None,
        "semantic_equivalence_verified": False,
    }

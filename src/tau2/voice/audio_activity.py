"""Shared PCM16 RMS detection and a clocked, response-scoped playback queue."""

import audioop
import base64
import time
from collections import deque

from tau2.config import AUDIO_ACTIVITY_FRAME_MS, AUDIO_ACTIVITY_RMS_THRESHOLD

VERSION = "tau-audio-activity/1"
USER_EVENT_VERSION = "tau-user-audio-events/1"
CONFIG = {
    "method": "rms",
    "frame_ms": AUDIO_ACTIVITY_FRAME_MS,
    "rms_threshold": AUDIO_ACTIVITY_RMS_THRESHOLD,
    "amplitude": "signed_pcm16",
}


class SpeechDetector:
    """Detect fixed windows across packet boundaries, without resampling or gain."""

    def __init__(self, rate, threshold=AUDIO_ACTIVITY_RMS_THRESHOLD):
        self.rate, self.threshold = rate, threshold
        self.window = rate * AUDIO_ACTIVITY_FRAME_MS // 1000 * 2
        self.pending = b""
        self.offset = 0

    def feed(self, pcm, final=False):
        """Return source-second intervals; a final partial window keeps its length."""
        self.pending += pcm
        intervals = []
        while len(self.pending) >= self.window or (final and self.pending):
            n = min(self.window, len(self.pending))
            frame, self.pending = self.pending[:n], self.pending[n:]
            start, end = (
                self.offset / (2 * self.rate),
                (self.offset + n) / (2 * self.rate),
            )
            if audioop.rms(frame, 2) > self.threshold:
                if intervals and abs(intervals[-1][1] - start) < 1e-9:
                    intervals[-1][1] = end
                else:
                    intervals.append([start, end])
            self.offset += n
        return intervals


def detect_speech(pcm, rate):
    """Measure a complete mono PCM16 segment using the shared detector."""
    return SpeechDetector(rate).feed(pcm, final=True)


class MediaPlayback:
    """Schedule audio at receipt, retaining true gaps and cutting only future audio.

    Playback is a real-time simulated presentation, not a hardware output device.
    The clock advances independently of task ticks; drain only takes a view.
    """

    def __init__(self, audio_format, clock=time.monotonic):
        self.format, self.clock = audio_format, clock
        self.queue = deque()
        self.played = {}

    def append(self, data, response_id=None, received=None):
        """Queue audio at the current playback cursor, never before receipt."""
        if not data:
            return
        received = self.clock() if received is None else received
        start = max(
            self.clock(), received, self.queue[-1]["end"] if self.queue else received
        )
        self.queue.append(
            dict(
                data=data,
                response_id=response_id,
                received=received,
                start=start,
                end=start + len(data) / self.format.bytes_per_second,
            )
        )

    def cancel(self, ids=None, at=None):
        """Remove unplayed samples of the selected responses, preserving other IDs."""
        at = self.clock() if at is None else at
        for row in self.queue:
            if ids is not None and row["response_id"] not in ids:
                continue
            count = max(
                0,
                min(
                    len(row["data"]),
                    int((at - row["start"]) * self.format.bytes_per_second),
                ),
            )
            width = self.format.bytes_per_sample
            count -= count % width
            row["data"] = row["data"][:count]
            row["end"] = row["start"] + count / self.format.bytes_per_second
        self.queue = deque(row for row in self.queue if row["data"])
        cursor = at
        for row in self.queue:
            if row["start"] >= at:
                start = max(at, cursor, row["received"])
                row.update(
                    start=start,
                    end=start + len(row["data"]) / self.format.bytes_per_second,
                )
            cursor = row["end"]

    def pending_chunks(self, at=None):
        """Return remaining response audio for protocol compatibility."""
        now = self.clock() if at is None else at
        chunks = []
        for row in self.queue:
            n = max(0, int((now - row["start"]) * self.format.bytes_per_second))
            n -= n % self.format.bytes_per_sample
            if n < len(row["data"]):
                chunks.append((row["data"][n:], row["response_id"]))
        return chunks

    def drain(self, until=None):
        """Take played samples with exact presentation times, without future audio."""
        until = self.clock() if until is None else until
        rows = []
        while self.queue and self.queue[0]["start"] < until:
            row = self.queue[0]
            count = min(
                len(row["data"]),
                int((until - row["start"]) * self.format.bytes_per_second),
            )
            count -= count % self.format.bytes_per_sample
            if count <= 0:
                break
            data = row["data"][:count]
            end = row["start"] + count / self.format.bytes_per_second
            rows.append(
                dict(
                    role="agent",
                    start=row["start"],
                    end=end,
                    received_monotonic=row["received"],
                    response_id=row["response_id"],
                    audio=base64.b64encode(data).decode(),
                    format=self.format.model_dump(mode="json"),
                )
            )
            rid = row["response_id"]
            self.played[rid] = self.played.get(rid, 0) + count
            if count == len(row["data"]):
                self.queue.popleft()
            else:
                row.update(data=row["data"][count:], start=end)
                break
        return rows


def merge_intervals(rows):
    """Join adjacent voiced windows, preserving actual silent gaps."""
    merged = []
    for a, b in sorted(rows):
        if merged and a <= merged[-1][1] + 1e-8:
            merged[-1][1] = max(b, merged[-1][1])
        else:
            merged.append([a, b])
    return merged


def speech_evidence(ticks):
    """Read shared media and source-classified user events, preserving history."""
    origin = None
    turns, events, agent, calls = {}, {}, [], []
    end, classified, saw_user = 0.0, True, False
    for index, tick in enumerate(ticks):
        raw = getattr(tick.agent_chunk, "raw_data", None) or {}
        if raw.get("media_origin") is None:
            continue
        if origin is None:
            origin = raw["media_origin"]
        end = max(end, raw.get("media_cutoff") or 0.0)
        agent.extend(raw.get("speech_intervals", []))
        calls.extend(raw.get("media_tool_calls", []))
        for row in raw.get("media_timeline", []):
            end = max(end, row["end"])
            if row["role"] != "user":
                continue
            saw_user = True
            classified &= row.get("user_event_version") == USER_EVENT_VERSION
            key = tuple(row.get("utterance_ids") or [f"tick-{index}"])
            kind = row.get("user_event_kind")
            pieces = row.get("speech_intervals", [])
            if pieces:
                turn = turns.setdefault(
                    key, dict(intervals=[], start_tick=index, end_tick=index + 1)
                )
                turn["intervals"].extend(pieces)
                turn["end_tick"] = index + 1
                if kind not in (None, "background", "control"):
                    event = events.setdefault(
                        (kind, key), dict(id="/".join(key), kind=kind, intervals=[])
                    )
                    event["intervals"].extend(pieces)
            for item in row.get("independent_user_events", []):
                event = events.setdefault(
                    (item["kind"], item["id"]),
                    dict(id=item["id"], kind=item["kind"], intervals=[]),
                )
                event["intervals"].extend(item["intervals"])
    if origin is None:
        return None

    def relative(rows):
        return [[a - origin, b - origin] for a, b in merge_intervals(rows)]

    return dict(
        origin=origin,
        duration=max(0.0, end - origin),
        user_turns=[
            dict(t, intervals=relative(t["intervals"])) for t in turns.values()
        ],
        agent_intervals=relative(agent),
        tool_calls=[dict(c, started_mono=c["started_mono"] - origin) for c in calls],
        user_event_version=USER_EVENT_VERSION if classified and saw_user else None,
        user_events=[
            dict(t, intervals=relative(t["intervals"])) for t in events.values()
        ],
    )


def is_speaking(chunk):
    """Current external speech state, distinct from any activity in a saved tick."""
    if chunk is None:
        return False
    return (getattr(chunk, "raw_data", None) or {}).get(
        "speaking_now", getattr(chunk, "contains_speech", False)
    )

"""Reply-window ownership and provider transcript classification."""

from __future__ import annotations


class ReplyWindowManager:
    """Collect reply evidence and classify it against released requests."""

    def __init__(self, config, record, diagnostic):
        self.config = config
        self._record = record
        self._diagnostic = diagnostic
        self.reply_fragments: list[dict] = []
        self.generated_fragments: list[dict] = []
        self.played_audio_segments: list[dict] = []
        self.played_replies: list[float] = []

    def add_reply(self, text, start, end):
        self._record("reply", text=text, start=start, end=end)
        self.reply_fragments.append({"text": text, "start": start, "end": end})

    def add_played_audio(self, start, end):
        if end <= start:
            return
        self._record("played_audio_segment", start=start, end=end)
        self.played_audio_segments.append({"start": start, "end": end})

    def add_generated_text(self, text, start, end, *, response_id=None, final=False):
        self._record(
            "generated_text",
            text=text,
            start=start,
            end=end,
            response_id=response_id,
            final=final,
        )
        if text:
            self.generated_fragments.append(
                {
                    "text": text,
                    "start": start,
                    "end": end,
                    "response_id": response_id,
                    "final": final,
                }
            )

    def add_agent_delivery(self, timestamp):
        self._record("agent_delivery", timestamp=timestamp)
        self.played_replies.append(timestamp)

    def build(self, rows, closed_at, stage_end):
        windows = {}
        starts = sorted(
            (row["delivery_started_at"], key)
            for key, row in rows.items()
            if row["delivery_started_at"] is not None
        )
        for index, (start, key) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else closed_at
            if stage_end is not None:
                end = min(end, stage_end)
            closed = rows[key].get("window_closed_at")
            if closed is not None:
                end = min(end, closed)
            fragments = [
                f
                for f in self.reply_fragments
                if f["start"] >= start and f["end"] <= end
            ]
            for fragment in self.reply_fragments:
                if fragment in fragments or not (
                    fragment["start"] < end and fragment["end"] > start
                ):
                    continue
                reason = (
                    "boundary_overlap_previous"
                    if fragment["start"] < start and fragment["end"] > start
                    else (
                        "boundary_overlap_next"
                        if fragment["start"] < end and fragment["end"] > end
                        else "unassigned_audio_fragment"
                    )
                )
                self._diagnostic(
                    reason,
                    request_id=key,
                    start=fragment["start"],
                    end=fragment["end"],
                )
            generated = [
                f
                for f in self.generated_fragments
                if f["start"] >= start and f["end"] <= end
            ]
            audio_segments = [
                segment
                for segment in self.played_audio_segments
                if segment["start"] < end and segment["end"] > start
            ]
            windows[key] = {
                "start": start,
                "end": end,
                "text": " ".join(
                    f["text"] for f in sorted(fragments, key=lambda f: f["start"])
                ),
                "generated_text": " ".join(
                    f["text"] for f in sorted(generated, key=lambda f: f["start"])
                ),
                "reply_source": self.config.reply_evidence_mode,
                "played_audio": bool(audio_segments),
                "audio_transcript_status": (
                    "completed"
                    if fragments
                    else ("missing" if audio_segments else "not_applicable")
                ),
                "played_audio_intervals": audio_segments,
                "response_ids": sorted(
                    {f["response_id"] for f in generated if f.get("response_id")}
                ),
            }
        covered = {
            id(fragment)
            for window in windows.values()
            for fragment in self.generated_fragments
            if fragment["start"] >= window["start"] and fragment["end"] <= window["end"]
        }
        prefetch = [f for f in self.generated_fragments if id(f) not in covered]
        return windows, " ".join(
            f["text"] for f in sorted(prefetch, key=lambda f: f["start"])
        )

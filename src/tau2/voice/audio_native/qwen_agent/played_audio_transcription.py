"""Shared asynchronous recognition and bounded review of already-played audio."""

import asyncio
import audioop
import contextlib
import hashlib
import time

import httpx

from tau2.config import DEFAULT_TELEPHONY_RATE
from tau2.data_model.transcript import LiveTranscriptFragment


class SegmentedPlayedAudioTranscriber:
    """Segment real playback; never block a media tick on a recognition request.

    Providers accept complete segments; timing uses measured playback bounds.
    Fragment timestamps therefore describe measured segment bounds, not forced
    word alignment. Only finished playback is queued; captions are never input.
    """

    provider = ""
    display_name = "ASR"
    output_sample_rate = 0

    def __init__(self, config, language="en", client=None):
        self.config, self.language = config, language
        self.model = config.asr_model
        self.key_env = config.asr_api_key_env
        self._client = client
        self._owns_client = client is None
        self.fragments = asyncio.Queue()
        self.failure = None
        self.worker = None
        self.sent_ms = 0.0
        self.latencies_ms = []
        self._segments = asyncio.Queue(maxsize=8)
        self._pending = bytearray()
        self._pending_start = 0.0
        self._silence_ms = 0.0
        self._last_speech_ms = 0.0
        self._clock_start = None
        self._last_speech_wall = None
        self._sequence = 0
        self._played_pcm = bytearray()
        self._fragment_ranges = {}
        self._review_cache = {}
        self._reviewed = set()
        self.reviews = []

    def validate_config(self):
        """Resolve only the evaluation-side key; do not forward it to Agent."""
        raise NotImplementedError

    async def _transcribe(self, pcm, *, purpose="primary"):
        """Recognize one measured segment with the selected provider."""
        raise NotImplementedError

    async def connect(self):
        """Start the observer after Agent boot, with independent HTTP work."""
        self.validate_config()
        self.fragments = asyncio.Queue()
        self.failure = None
        self.sent_ms = 0.0
        self.latencies_ms = []
        self._segments = asyncio.Queue(maxsize=8)
        self._pending.clear()
        self._pending_start = self._silence_ms = self._last_speech_ms = 0.0
        self._clock_start = None
        self._last_speech_wall = None
        self._sequence = 0
        self._played_pcm.clear()
        self._fragment_ranges.clear()
        self._review_cache.clear()
        self._reviewed.clear()
        self.reviews.clear()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30)
        self.worker = asyncio.create_task(self._recognize())

    async def send(self, pcm):
        """Consume PCM16 mono at the framework's played 8 kHz clock."""
        if self.failure:
            raise RuntimeError(
                f"{self.display_name} observer transcription unavailable"
            ) from self.failure
        if not pcm:
            return
        start_ms = self.sent_ms
        duration_ms = len(pcm) / (DEFAULT_TELEPHONY_RATE * 2) * 1000
        if self._clock_start is None:
            self._clock_start = time.monotonic() - duration_ms / 1000
        self.sent_ms += duration_ms
        if self.config.asr_recheck_enabled:
            self._played_pcm.extend(pcm)
            limit = (
                int(self.config.asr_recheck_buffer_seconds * DEFAULT_TELEPHONY_RATE) * 2
            )
            if len(self._played_pcm) > limit:
                del self._played_pcm[:-limit]
            oldest = self.sent_ms - len(self._played_pcm) / 16
            self._fragment_ranges = {
                key: value
                for key, value in self._fragment_ranges.items()
                if value["end_ms"] >= oldest
            }
        speech = audioop.rms(pcm, 2) >= self.config.asr_speech_rms
        if not self._pending:
            if not speech:
                return
            self._pending_start = start_ms
        self._pending.extend(pcm)
        if speech:
            self._silence_ms = 0
            self._last_speech_ms = self.sent_ms
            self._last_speech_wall = time.monotonic()
        else:
            self._silence_ms += duration_ms
        if (
            self.sent_ms - self._pending_start >= self.config.asr_segment_seconds * 1000
            or self._silence_ms >= self.config.asr_silence_seconds * 1000
        ):
            self._queue_pending()

    def _queue_pending(self):
        if not self._pending:
            return
        item = (
            bytes(self._pending),
            self._pending_start,
            self._last_speech_ms,
            self._last_speech_wall,
        )
        try:
            self._segments.put_nowait(item)
        except asyncio.QueueFull:
            raise RuntimeError(
                f"{self.display_name} ASR cannot keep up with playback"
            ) from None
        self._pending.clear()
        self._silence_ms = 0

    async def _recognize(self):
        try:
            while True:
                pcm, start_ms, end_ms, played_end_wall = await self._segments.get()
                try:
                    text = await self._transcribe(pcm)
                    if end_ms > self.sent_ms:
                        raise RuntimeError(
                            f"{self.display_name} transcript exceeds played audio"
                        )
                    latency = max(0, (time.monotonic() - played_end_wall) * 1000)
                    self.latencies_ms.append(latency)
                    self._sequence += 1
                    if text:
                        event_id = f"{self.provider}-asr-{self._sequence}"
                        if self.config.asr_recheck_enabled:
                            self._fragment_ranges[event_id] = {
                                "event_id": event_id,
                                "text": text,
                                "start_ms": start_ms,
                                "end_ms": end_ms,
                            }
                        self.fragments.put_nowait(
                            LiveTranscriptFragment(
                                event_id=event_id,
                                speaker="agent",
                                text=text + " ",
                                start_ms=round(start_ms),
                                end_ms=round(end_ms),
                            )
                        )
                finally:
                    self._segments.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = error

    def drain(self):
        """Release only recognition results completed by this playback tick."""
        if self.failure:
            raise RuntimeError(
                f"{self.display_name} observer transcription unavailable"
            ) from self.failure
        fragments = []
        while not self.fragments.empty():
            fragments.append(self.fragments.get_nowait())
        return fragments

    def summary(self):
        """Record the segment method and its actual release delay explicitly."""
        return {
            "provider": self.provider,
            "model": self.model,
            "mode": "played_audio_segments",
            "latency_basis": "wall_clock_since_played_segment_end",
            "timestamp_source": "local_segment_bounds",
            "word_timestamps": False,
            "input_sample_rate": self.output_sample_rate,
            "segment_seconds": self.config.asr_segment_seconds,
            "silence_seconds": self.config.asr_silence_seconds,
            "finalized_segments": len(self.latencies_ms),
            "mean_ms": sum(self.latencies_ms) / len(self.latencies_ms)
            if self.latencies_ms
            else None,
            "max_ms": max(self.latencies_ms) if self.latencies_ms else None,
            "pending_segments": self._segments.qsize(),
            "recheck_enabled": self.config.asr_recheck_enabled,
            "rechecks": list(self.reviews),
        }

    async def recheck(self, evidence: str, fragments: list[dict]) -> dict:
        """Review one bounded piece of already-heard audio, never provider captions.

        Caller-supplied times are not trusted. Known fragment IDs select measured
        playback bounds, and the snapshot's last fragment caps all context audio.
        Review text is supplementary evidence, never a replacement transcript.
        """
        if not self.config.asr_recheck_enabled:
            return {"status": "unavailable"}
        identifiers = {f.get("event_id") for f in fragments}
        known = sorted(
            (r for key, r in self._fragment_ranges.items() if key in identifiers),
            key=lambda r: r["start_ms"],
        )
        text = " ".join(" ".join(r["text"].split()) for r in known)
        quote = " ".join(evidence.split())
        if not quote or quote not in text:
            return {"status": "unavailable"}
        begin = text.rindex(quote)
        finish = begin + len(quote)
        matched, offset = [], 0
        for row in known:
            length = len(" ".join(row["text"].split()))
            if offset < finish and offset + length > begin:
                matched.append(row)
            offset += length + 1
        key = tuple(r["event_id"] for r in matched)
        if key in self._review_cache:
            return {**self._review_cache[key], "cached": True}
        if key in self._reviewed:
            return {"status": "unavailable"}
        self._reviewed.add(key)
        padding = self.config.asr_recheck_context_seconds * 1000
        start = max(0, matched[0]["start_ms"] - padding)
        end = min(known[-1]["end_ms"], matched[-1]["end_ms"] + padding, self.sent_ms)
        oldest = self.sent_ms - len(self._played_pcm) / 16
        result = {
            "fragment_ids": list(key),
            "start_ms": start,
            "end_ms": end,
            "original_text": " ".join(
                r["text"] for r in known if r["start_ms"] < end and r["end_ms"] > start
            ),
            "status": "unavailable",
        }
        if (
            start >= oldest
            and 0 < end - start <= self.config.asr_recheck_max_seconds * 1000
        ):
            pcm = bytes(
                self._played_pcm[
                    round((start - oldest) * 16) : round((end - oldest) * 16)
                ]
            )
            result["audio_sha256"] = hashlib.sha256(pcm).hexdigest()
            started = time.monotonic()
            try:
                review = await asyncio.wait_for(
                    self._transcribe(pcm, purpose="recheck"),
                    timeout=self.config.asr_recheck_timeout_seconds,
                )
                result.update(status="ok", review_text=review)
            except asyncio.TimeoutError:
                result["status"] = "timeout"
            except Exception:
                result["status"] = "error"
            result["latency_seconds"] = time.monotonic() - started
        self._review_cache[key] = result
        self.reviews.append(result)
        return dict(result)

    async def close(self):
        """Stop inference work; never feed final post-call text back to a user."""
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self.worker = None

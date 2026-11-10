"""Observer ASR: only audio already played to the simulated caller is visible."""

import asyncio
import contextlib
import json
import os
from urllib.parse import urlencode

import websockets

from tau2.data_model.transcript import LiveTranscriptFragment


class PlayedAudioTranscriber:
    """Stream the complete playback clock, including silence, to Deepgram."""

    def __init__(self, model, key_env, language="en"):
        self.model, self.key_env, self.language = model, key_env, language
        self.fragments = asyncio.Queue()
        self.failure = None
        self.ws = None
        self.reader = None
        self.sent_ms = 0.0
        self.sequence = 0
        self.clock_start = None
        self.latencies_ms = []

    def validate_config(self):
        """Fail before agent startup if the observer credential is missing."""
        if not os.environ.get(self.key_env):
            raise ValueError(f"Missing environment variable {self.key_env}")

    async def connect(self):
        """Authenticate after the agent boots, avoiding an idle ASR timeout."""
        self.failure = None
        self.fragments = asyncio.Queue()
        self.sent_ms = 0.0
        self.sequence = 0
        self.clock_start = None
        self.latencies_ms = []
        key = os.environ.get(self.key_env)
        if not key:
            raise ValueError(f"Missing environment variable {self.key_env}")
        query = urlencode(
            {
                "model": self.model,
                "language": self.language,
                "encoding": "linear16",
                "sample_rate": 8000,
                "channels": 1,
                "interim_results": "false",
                "punctuate": "true",
                "endpointing": 200,
            }
        )
        self.ws = await websockets.connect(
            f"wss://api.deepgram.com/v1/listen?{query}",
            additional_headers={"Authorization": f"Token {key}"},
        )
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                if event.get("type") == "Error":
                    raise RuntimeError("Observer transcription failed")
                if event.get("type") != "Results" or not event.get("is_final"):
                    continue
                alternative = event["channel"]["alternatives"][0]
                text = alternative.get("transcript", "").strip()
                if not text:
                    continue
                self.sequence += 1
                start = float(event.get("start", 0))
                end = start + float(event.get("duration", 0))
                if end * 1000 > self.sent_ms + 1:
                    raise RuntimeError("Observer transcript exceeds played audio")
                if self.clock_start is not None:
                    self.latencies_ms.append(
                        max(
                            0,
                            (asyncio.get_running_loop().time() - self.clock_start - end)
                            * 1000,
                        )
                    )
                words = alternative.get("words") or [
                    {"start": start, "end": end, "word": text}
                ]
                for index, word in enumerate(words):
                    await self.fragments.put(
                        LiveTranscriptFragment(
                            event_id=f"asr-{self.sequence}-{index}",
                            speaker="agent",
                            text=(word.get("punctuated_word") or word["word"]) + " ",
                            start_ms=round(word["start"] * 1000),
                            end_ms=round(word["end"] * 1000),
                        )
                    )
            if self.ws is not None:
                self.failure = RuntimeError("Observer transcription disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc

    async def send(self, pcm):
        """Accept PCM16 8 kHz only after playback has advanced."""
        if self.failure:
            raise RuntimeError("Observer transcription unavailable") from self.failure
        if self.clock_start is None:
            self.clock_start = asyncio.get_running_loop().time() - len(pcm) / 16000
        self.sent_ms += len(pcm) / 16
        await self.ws.send(pcm)

    def drain(self):
        """Return newly finalized words; never use speculative Gateway text."""
        if self.failure:
            raise RuntimeError("Observer transcription unavailable") from self.failure
        fragments = []
        while not self.fragments.empty():
            fragments.append(self.fragments.get_nowait())
        return fragments

    async def flush_pending(self):
        """Drain finalized streaming results before closing the websocket."""
        if self.failure:
            raise RuntimeError("Observer transcription unavailable") from self.failure
        await asyncio.sleep(0)
        return self.drain()

    def summary(self):
        """Observer finalization latency, separate from Agent playback metrics."""
        return {
            "finalized_segments": len(self.latencies_ms),
            "mean_ms": sum(self.latencies_ms) / len(self.latencies_ms)
            if self.latencies_ms
            else None,
            "max_ms": max(self.latencies_ms) if self.latencies_ms else None,
        }

    async def close(self):
        """Stop the observer, retaining all finalized evidence already received."""
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.ws:
            await self.ws.close()
        self.ws = None

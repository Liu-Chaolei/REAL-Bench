"""Audio review selects exact PCM bytes at native and legacy playback rates."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from tau2.adapters.qwen_audio_agent.transcription.played_audio import (
    SegmentedPlayedAudioTranscriber,
)
from tau2.adapters.qwen_audio_agent.transcription.qwen_streaming import (
    QwenStreamingPlayedAudioTranscriber,
)


@pytest.mark.parametrize("sample_rate", [None, 8000, 16000, 24000, 48000])
@pytest.mark.parametrize("buffer_start_ms", [0, 60000])
def test_recheck_selects_requested_pcm_at_actual_rate(sample_rate, buffer_start_ms):
    """Include preceding audio, trailing silence and an evicted buffer prefix."""
    config = SimpleNamespace(
        asr_model="unused",
        asr_api_key_env="UNUSED_KEY",
        asr_hotwords=SimpleNamespace(enabled=False),
        asr_recheck_enabled=True,
        asr_recheck_context_seconds=0,
        asr_recheck_max_seconds=20,
        asr_recheck_timeout_seconds=1,
    )
    if sample_rate is None:
        observer = SegmentedPlayedAudioTranscriber(config)
    else:
        observer = QwenStreamingPlayedAudioTranscriber(config)
        observer.played_sample_rate = sample_rate
    rate = sample_rate or 8000
    samples = rate // 5  # Each region is 200 ms of mono PCM16.
    speech = b"\x13\x37" * samples
    observer._played_pcm.extend(b"\x11\x22" * samples + speech + bytes(samples * 2))
    observer.sent_ms = buffer_start_ms + 600
    start, end = buffer_start_ms + 200, buffer_start_ms + 400
    fragment = {
        "event_id": "spoken",
        "text": "Book at noon",
        "start_ms": start,
        "end_ms": end,
    }
    observer._fragment_ranges["spoken"] = fragment
    captured = []

    async def recognize(pcm, *, purpose):
        assert purpose == "recheck"
        captured.append(pcm)
        return "Book at noon"

    observer._transcribe = recognize
    result = asyncio.run(observer.recheck("Book at noon", [fragment]))

    assert result["status"] == "ok"
    assert captured == [speech]
    assert result["start_ms"] == start
    assert result["end_ms"] == end
    assert result["audio_sha256"] == hashlib.sha256(speech).hexdigest()

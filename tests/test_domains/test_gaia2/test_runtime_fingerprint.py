"""Playback and transcript changes must invalidate sealed Gaia2 run bindings."""

from pathlib import Path

import pytest

from tau2.domains.gaia2.utils import runtime_digest


@pytest.mark.parametrize(
    "suffix",
    [
        "adapters/qwen_audio_agent/adapter.py",
        "voice/audio_native/adapter.py",
        "voice/audio_native/tick_result.py",
        "utils/transcript/live.py",
        "utils/transcript/views.py",
        "utils/transcript/audio_text.py",
        "diagnostics/collect.py",
        "data_model/transcript.py",
    ],
)
def test_runtime_fingerprint_covers_playback_and_transcript_semantics(
    monkeypatch, suffix
):
    before = runtime_digest()
    original = Path.read_bytes

    def altered(path):
        value = original(path)
        return (
            value + b"\n# simulated runtime change\n"
            if path.as_posix().endswith(suffix)
            else value
        )

    monkeypatch.setattr(Path, "read_bytes", altered)
    assert runtime_digest() != before

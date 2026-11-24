"""Mechanism tests for the SAASR interaction-insert path."""

import numpy as np

from tau2.data_model.audio import AudioData, AudioEncoding, AudioFormat
from tau2.data_model.audio_effects import SpeechEffectsConfig, UserSpeechInsert
from tau2.data_model.voice import SynthesisConfig
from tau2.voice import interaction_inserts
from tau2.voice.interaction_inserts import InteractionInsertEngine


class FakeInsertGenerator:
    def __init__(self, **kwargs):
        self.audio = AudioData(
            data=(np.sin(np.arange(6400) / 17) * 5000).astype("<i2").tobytes(),
            format=AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000),
        )

    def generate_all(self, items):
        self.items = items

    def get_audio(self, item):
        return self.audio


def test_interaction_insert_engine_only_plays_during_gaps(monkeypatch):
    monkeypatch.setattr(
        interaction_inserts, "OutOfTurnSpeechGenerator", FakeInsertGenerator
    )
    item = UserSpeechInsert(text="Hold on a second.", type="non_directed_phrase")
    speech_config = SpeechEffectsConfig(
        enable_vocal_tics=False,
        enable_non_directed_phrases=True,
        non_directed_phrases=[item],
        speech_insert_events_per_minute=6000,
    )
    engine = InteractionInsertEngine(
        synthesis_config=SynthesisConfig(),
        speech_config=speech_config,
        persona_name="default",
        voice_id=None,
        seed=42,
    )

    first = engine.process(is_speech=False, num_samples=1600, pending=None)
    assert first.triggered == item
    assert np.any(first.audio)
    assert first.pending is not None

    interrupted = engine.process(
        is_speech=True, num_samples=1600, pending=first.pending
    )
    assert interrupted.discarded
    assert interrupted.pending is None
    assert not np.any(interrupted.audio)

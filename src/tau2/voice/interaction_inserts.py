"""Interactive speech inserts for the SAASR caller renderer.

This module owns only short out-of-turn caller inserts.  It deliberately does
not depend on Tau's continuous noise, burst-noise, frame-drop, or telephony
processing paths.
"""

from __future__ import annotations

import random
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

import numpy as np

from tau2.config import DEFAULT_TELEPHONY_RATE
from tau2.data_model.audio_effects import UserSpeechInsert
from tau2.data_model.voice import SynthesisConfig
from tau2.voice.synthesis.audio_effects.speech_generator import OutOfTurnSpeechGenerator
from tau2.voice.utils.audio_preprocessing import apply_fade_out, resample_audio
from tau2.voice.utils.probability import poisson_should_trigger


@dataclass(frozen=True)
class PendingInteractionInsert:
    """A short insert that is being delivered over one or more audio ticks."""

    audio_bytes: bytes
    offset: int = 0
    info: Optional[dict] = None

    @property
    def remaining_bytes(self) -> int:
        return len(self.audio_bytes) - self.offset


@dataclass(frozen=True)
class InteractionInsertResult:
    """One tick of insert audio and its state transition metadata."""

    audio: np.ndarray
    pending: Optional[PendingInteractionInsert]
    triggered: Optional[UserSpeechInsert] = None
    completed: bool = False
    discarded: bool = False
    event: Optional[dict] = None


class InteractionInsertEngine:
    """Schedule and render speech inserts during caller interaction gaps."""

    def __init__(
        self,
        *,
        synthesis_config: SynthesisConfig,
        speech_config,
        persona_name: str,
        voice_id: Optional[str],
        sample_rate: int = DEFAULT_TELEPHONY_RATE,
        seed: int = 42,
    ):
        self.sample_rate = sample_rate
        self.speech_config = speech_config
        self.items = speech_config.get_out_of_turn_speech_inserts()
        self.rate = speech_config.speech_insert_events_per_minute / 60.0
        self.trigger_rng = random.Random(seed)
        self.selection_rng = random.Random(seed + 1)
        self.generator: Optional[OutOfTurnSpeechGenerator] = None

        if not self.items or self.rate <= 0:
            return

        provider_config = deepcopy(synthesis_config.provider_config)
        if provider_config is None:
            raise ValueError("Interaction inserts require a TTS provider configuration")
        if voice_id is not None and hasattr(provider_config, "voice_id"):
            provider_config.voice_id = voice_id
        self.generator = OutOfTurnSpeechGenerator(
            voice_id=voice_id or "interaction-insert",
            provider=synthesis_config.provider,
            provider_config=provider_config,
            target_sample_rate=sample_rate,
            seed=seed,
            speech_rate=self.rate,
        )
        self.generator.generate_all(self.items)

    def _select_item(self, duration_seconds: float) -> Optional[UserSpeechInsert]:
        if not self.items or self.rate <= 0:
            return None
        if not poisson_should_trigger(self.rate, duration_seconds, self.trigger_rng):
            return None
        return self.selection_rng.choice(self.items)

    def _audio_for(self, item: UserSpeechInsert) -> Optional[bytes]:
        if self.generator is None:
            return None
        audio = self.generator.get_audio(item)
        if audio is None:
            return None
        # Keep the insert renderer independent from the main SAASR format while
        # allowing providers with a different PCM sample rate.
        if audio.format.sample_rate != self.sample_rate:
            audio = resample_audio(audio, self.sample_rate)
        return audio.data

    def process(
        self,
        *,
        is_speech: bool,
        num_samples: int,
        pending: Optional[PendingInteractionInsert],
    ) -> InteractionInsertResult:
        """Render one tick, cancelling inserts when formal speech begins."""
        discarded = False
        if is_speech and pending is not None:
            pending = None
            discarded = True

        triggered = None
        if not is_speech and pending is None:
            item = self._select_item(num_samples / self.sample_rate)
            if item is not None:
                audio = self._audio_for(item)
                if audio:
                    triggered = item
                    pending = PendingInteractionInsert(
                        audio_bytes=audio,
                        info={
                            "id": str(uuid.uuid4()),
                            "type": item.type,
                            "text": item.text,
                        },
                    )

        output = np.zeros(num_samples, dtype=np.float64)
        completed = False
        event = None
        if pending is not None and not is_speech and pending.remaining_bytes > 0:
            bytes_per_sample = 2
            portion_length = min(
                num_samples * bytes_per_sample, pending.remaining_bytes
            )
            portion = np.frombuffer(
                pending.audio_bytes[pending.offset : pending.offset + portion_length],
                dtype="<i2",
            ).astype(np.float64)
            if len(portion) < num_samples:
                portion = apply_fade_out(portion)
                portion = np.pad(portion, (0, num_samples - len(portion)))
            output[: min(num_samples, len(portion))] = portion[:num_samples] / 32767.0
            new_offset = pending.offset + portion_length
            completed = new_offset >= len(pending.audio_bytes)
            event = {
                **(pending.info or {}),
                "range_seconds": [
                    0.0,
                    min(num_samples * bytes_per_sample, pending.remaining_bytes)
                    / (bytes_per_sample * self.sample_rate),
                ],
            }
            pending = (
                None
                if completed
                else PendingInteractionInsert(
                    audio_bytes=pending.audio_bytes,
                    offset=new_offset,
                    info=pending.info,
                )
            )

        return InteractionInsertResult(
            audio=output,
            pending=pending,
            triggered=triggered,
            completed=completed,
            discarded=discarded,
            event=event,
        )

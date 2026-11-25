"""MiMo recognition using the shared played-audio observer lifecycle."""

from tau2.adapters.qwen_audio_agent.transcription.played_audio import (
    SegmentedPlayedAudioTranscriber,
)
from tau2.config import DEFAULT_MIMO_ASR_SAMPLE_RATE, DEFAULT_TELEPHONY_RATE
from tau2.voice.mimo import mimo_api_key, transcribe_mimo


class MiMoPlayedAudioTranscriber(SegmentedPlayedAudioTranscriber):
    """Retain MiMo's public observer interface and its 24 kHz recognition API."""

    provider = "mimo"
    display_name = "MiMo"
    output_sample_rate = DEFAULT_MIMO_ASR_SAMPLE_RATE

    def validate_config(self):
        """Resolve only the observer credential before Agent startup."""
        self._key = mimo_api_key(self.key_env, self.config.config_env_file)

    async def _transcribe(self, pcm, *, purpose="primary"):
        return await transcribe_mimo(
            self._client,
            self.config.asr_base_url,
            self.model,
            self._key,
            pcm,
            DEFAULT_TELEPHONY_RATE,
            self.language,
        )

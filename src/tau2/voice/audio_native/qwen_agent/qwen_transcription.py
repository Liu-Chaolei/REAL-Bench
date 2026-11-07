"""Private user-side Qwen ASR of actual playback with one fixed vocabulary."""

from tau2.config import DEFAULT_QWEN_ASR_SAMPLE_RATE, DEFAULT_TELEPHONY_RATE
from tau2.utils.env_config import read_config_environment, resolve_env_reference
from tau2.voice.audio_native.qwen_agent.played_audio_transcription import (
    SegmentedPlayedAudioTranscriber,
)
from tau2.voice.qwen_asr import qwen_asr_url, transcribe_qwen


class QwenPlayedAudioTranscriber(SegmentedPlayedAudioTranscriber):
    """Keep the observer credential and user background outside the Agent."""

    provider = "qwen"
    display_name = "Qwen"
    output_sample_rate = DEFAULT_QWEN_ASR_SAMPLE_RATE

    def __init__(self, config, language="en", client=None, hotwords=None):
        super().__init__(config, language, client)
        self.hotwords = hotwords if config.asr_hotwords.enabled else None
        self.usage_records = []

    def validate_config(self):
        """Validate the private observer profile before billable Agent startup."""
        values = read_config_environment(self.config.config_env_file)
        if self.config.asr_provider_env:
            if (
                resolve_env_reference(values, self.config.asr_provider_env)
                != "dashscope"
            ):
                raise ValueError("Qwen observer requires the dashscope provider")
        self._key = resolve_env_reference(values, self.key_env)
        if not self._key:
            raise ValueError(f"Missing environment variable {self.key_env}")
        base = (
            resolve_env_reference(values, self.config.asr_base_url_env)
            if self.config.asr_base_url_env
            else self.config.asr_base_url
        )
        self._url = qwen_asr_url(base)
        if self.config.asr_hotwords.enabled and self.hotwords is None:
            raise ValueError(
                "Automatic ASR hotwords require a prepared per-task vocabulary"
            )

    async def connect(self):
        self.usage_records.clear()
        await super().connect()

    async def _transcribe(self, pcm, *, purpose="primary"):
        result = await transcribe_qwen(
            self._client,
            self._url,
            self.model,
            self._key,
            pcm,
            DEFAULT_TELEPHONY_RATE,
            self.language,
            self.hotwords.vocabulary() if self.hotwords is not None else None,
        )
        self.usage_records.append({"purpose": purpose, **result.usage})
        return result.text

    def summary(self):
        """Persist exactly the vocabulary and provider usage used in this call."""
        return {
            **super().summary(),
            "hotwords": self.hotwords.record()
            if self.hotwords is not None
            else {"enabled": False},
            "usage": list(self.usage_records),
        }

"""DashScope Qwen-Audio speech synthesis with private credential references."""

import audioop
import base64
import binascii
import io
import wave
from urllib.parse import urlsplit, urlunsplit

import httpx

from tau2.config import DEFAULT_PCM_SAMPLE_RATE
from tau2.data_model.audio import AudioData, AudioEncoding, AudioFormat
from tau2.data_model.voice import QwenTTSConfig
from tau2.utils.env_config import read_config_environment, resolve_env_reference


class QwenTTSConnectionError(ConnectionError):
    """Sanitized, retryable transport failure from the Qwen TTS endpoint."""


def qwen_tts_api_key(config: QwenTTSConfig) -> str:
    """Resolve the configured Qwen credential without exposing its value."""
    values = read_config_environment(config.config_env_file)
    key = resolve_env_reference(values, config.api_key_env)
    if not key:
        raise ValueError(f"Missing environment variable {config.api_key_env}")
    return key


def qwen_tts_base_url(config: QwenTTSConfig) -> str:
    """Resolve the endpoint base from the explicit URL or private dotenv file."""
    if config.base_url:
        return config.base_url
    values = read_config_environment(config.config_env_file)
    base_url = resolve_env_reference(values, config.base_url_env)
    if not base_url:
        raise ValueError(f"Missing environment variable {config.base_url_env}")
    return base_url


def qwen_tts_url(base_url: str) -> str:
    """Map a DashScope-compatible base URL to the Qwen-Audio TTS endpoint."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid Qwen TTS base URL")
    path = parsed.path.rstrip("/")
    endpoint = "/api/v1/services/audio/tts/SpeechSynthesizer"
    if path.endswith(endpoint):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    for suffix in (
        "/compatible-mode/v1",
        "/api-ws/v1/realtime",
        "/api-ws/v1/inference",
        "/api/v1",
    ):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    else:
        if path:
            raise ValueError("Unsupported Qwen TTS base URL path")
    return urlunsplit((parsed.scheme, parsed.netloc, path + endpoint, "", ""))


def _language_name(language_code: str | None) -> str | None:
    """Translate tau's short language code to DashScope's language spelling."""
    if not language_code:
        return None
    return {
        "en": "English",
        "zh": "Chinese",
        "de": "German",
        "es": "Spanish",
        "fr": "French",
        "it": "Italian",
        "ja": "Japanese",
        "ko": "Korean",
        "pt": "Portuguese",
        "ru": "Russian",
    }.get(language_code.split("-", 1)[0].lower(), language_code)


def _decode_wav(raw: bytes) -> tuple[bytes, int]:
    """Validate a provider WAV and return mono PCM16 plus its sample rate."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                raise ValueError
            channels, sample_rate = reader.getnchannels(), reader.getframerate()
            if channels not in (1, 2) or not 8000 <= sample_rate <= 48000:
                raise ValueError
            pcm = reader.readframes(reader.getnframes())
    except (EOFError, ValueError, wave.Error):
        raise RuntimeError("Qwen TTS returned invalid WAV audio") from None
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if not pcm:
        raise RuntimeError("Qwen TTS returned empty audio")
    return pcm, sample_rate


def _request_audio(text: str, config: QwenTTSConfig, key: str) -> tuple[bytes, int]:
    """Request one bounded text segment and download its returned WAV."""
    payload = {
        "model": config.model_id,
        "input": {
            "text": text,
            "voice": config.voice_id,
            "format": "wav",
            "sample_rate": config.sample_rate,
        },
    }
    language = _language_name(config.language_code)
    if language:
        payload["input"]["language_type"] = language
    try:
        with httpx.Client(timeout=config.request_timeout_seconds) as client:
            response = client.post(
                qwen_tts_url(qwen_tts_base_url(config)),
                headers={"Authorization": "Bearer " + key},
                json=payload,
            )
            if not response.is_success:
                raise RuntimeError(f"Qwen TTS failed (HTTP {response.status_code})")
            try:
                audio = response.json()["output"]["audio"]
                encoded = audio.get("data")
                audio_url = audio.get("url")
                if encoded:
                    raw = base64.b64decode(encoded, validate=True)
                elif isinstance(audio_url, str) and audio_url:
                    download = client.get(audio_url)
                    if not download.is_success:
                        raise RuntimeError(
                            f"Qwen TTS audio download failed (HTTP {download.status_code})"
                        )
                    raw = download.content
                else:
                    raise ValueError
            except (KeyError, TypeError, ValueError, binascii.Error):
                raise RuntimeError(
                    "Qwen TTS returned an invalid audio response"
                ) from None
    except httpx.HTTPError:
        raise QwenTTSConnectionError("Qwen TTS connection failed") from None
    return _decode_wav(raw)


def _text_segments(text: str, limit: int = 600) -> list[str]:
    """Split long utterances at punctuation so Qwen's 600-character limit holds."""
    if len(text) <= limit:
        return [text]
    segments = []
    remaining = text.strip()
    while len(remaining) > limit:
        cut = max(
            remaining.rfind(mark, 1, limit + 1)
            for mark in (".", "!", "?", ",", ";", " ")
        )
        cut = cut if cut > 0 else limit
        segments.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        segments.append(remaining)
    return segments


def synthesize_qwen(text: str, config: QwenTTSConfig) -> AudioData:
    """Synthesize text with Qwen-Audio-TTS and normalize to tau's PCM16 16 kHz."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Qwen TTS requires nonempty text")
    key = qwen_tts_api_key(config)
    chunks = []
    for segment in _text_segments(text):
        pcm, rate = _request_audio(segment, config, key)
        chunks.append((pcm, rate))
    data = b"".join(
        audioop.ratecv(pcm, 2, 1, rate, DEFAULT_PCM_SAMPLE_RATE, None)[0]
        if rate != DEFAULT_PCM_SAMPLE_RATE
        else pcm
        for pcm, rate in chunks
    )
    if not data:
        raise RuntimeError("Qwen TTS returned empty audio")
    return AudioData(
        data=data,
        format=AudioFormat(
            encoding=AudioEncoding.PCM_S16LE, sample_rate=DEFAULT_PCM_SAMPLE_RATE
        ),
    )

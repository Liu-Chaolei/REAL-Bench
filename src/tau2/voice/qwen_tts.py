"""Qwen Audio 3.1 non-realtime TTS adapter."""

from __future__ import annotations

import audioop
import base64
import io
import wave

import httpx

from tau2.config import DEFAULT_PCM_SAMPLE_RATE
from tau2.data_model.audio import AudioData, AudioEncoding, AudioFormat
from tau2.data_model.voice import QwenTTSConfig
from tau2.utils.env_config import read_config_environment, resolve_env_reference


def _resolve_key(config: QwenTTSConfig) -> str:
    key = resolve_env_reference(
        read_config_environment(config.config_env_file), config.api_key_env
    )
    if not key:
        raise ValueError(f"Missing environment variable {config.api_key_env}")
    return key


def qwen_tts_url(config: QwenTTSConfig) -> str:
    """Convert a configured DashScope base URL to the HTTP TTS endpoint."""
    base = config.base_url.rstrip("/")
    if "/api/v1/services/audio/tts/SpeechSynthesizer" in base:
        return base
    for suffix in ("/api-ws/v1/realtime", "/api/v1", "/compatible-mode/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base + "/api/v1/services/audio/tts/SpeechSynthesizer"


def synthesize_qwen(text: str, config: QwenTTSConfig) -> AudioData:
    """Synthesize one utterance through Qwen Audio TTS HTTP API."""
    if not text.strip():
        raise ValueError("Qwen TTS text must not be empty")
    key = _resolve_key(config)
    try:
        with httpx.Client(timeout=config.request_timeout_seconds) as client:
            payload = {
                "model": config.model_id,
                "input": {
                    "text": text,
                    "voice": config.voice_id,
                    "format": "wav",
                    "sample_rate": config.sample_rate,
                },
            }
            if config.language_code:
                payload["input"]["language_hints"] = [config.language_code]
            response = client.post(
                qwen_tts_url(config),
                headers={"Authorization": "Bearer " + key},
                json=payload,
            )
            if not response.is_success:
                try:
                    error_body = response.json()
                    code = error_body.get("code", "unknown")
                except (ValueError, TypeError):
                    code = "unknown"
                raise RuntimeError(
                    f"Qwen TTS failed (HTTP {response.status_code}, code={code}, "
                    f"model={config.model_id}, voice={config.voice_id})"
                )
            output = (response.json().get("output") or {}).get("audio") or {}
            encoded = output.get("data")
            if encoded:
                raw = base64.b64decode(encoded, validate=True)
            elif output.get("url"):
                download = client.get(output["url"])
                download.raise_for_status()
                raw = download.content
            else:
                raise ValueError("Qwen TTS response has no audio")
    except httpx.HTTPError as error:
        raise RuntimeError(
            f"Qwen TTS HTTP request failed ({type(error).__name__})"
        ) from None
    except (ValueError, TypeError):
        raise RuntimeError("Qwen TTS returned invalid audio") from None
    try:
        with wave.open(io.BytesIO(raw)) as reader:
            channels = reader.getnchannels()
            rate = reader.getframerate()
            width = reader.getsampwidth()
            # Some DashScope WAV responses keep a streaming-sized RIFF length
            # in the header. Bound reads by the actual payload as well.
            max_frames = len(raw) // max(1, channels * width)
            audio = reader.readframes(min(reader.getnframes(), max_frames))
        if channels != 1 or width != 2 or not audio:
            raise ValueError
        if rate != DEFAULT_PCM_SAMPLE_RATE:
            audio, _ = audioop.ratecv(audio, 2, 1, rate, DEFAULT_PCM_SAMPLE_RATE, None)
    except (ValueError, wave.Error, EOFError):
        raise RuntimeError("Qwen TTS returned invalid PCM16 WAV audio") from None
    return AudioData(
        data=audio,
        format=AudioFormat(
            encoding=AudioEncoding.PCM_S16LE,
            sample_rate=DEFAULT_PCM_SAMPLE_RATE,
        ),
    )

"""Xiaomi MiMo audio API boundary, with private credential references."""

import audioop
import base64
import io
import wave

import httpx

from tau2.config import DEFAULT_MIMO_ASR_SAMPLE_RATE, DEFAULT_PCM_SAMPLE_RATE
from tau2.data_model.audio import AudioData, AudioEncoding, AudioFormat
from tau2.data_model.voice import MiMoTTSConfig
from tau2.utils.env_config import read_config_environment, resolve_env_reference


class MiMoTTSConnectionError(ConnectionError):
    """Sanitized, retryable transport failure from the MiMo TTS endpoint."""


def mimo_api_key(key_env: str, config_env_file: str | None) -> str:
    """Resolve one credential without exporting unrelated evaluation secrets."""
    value = resolve_env_reference(read_config_environment(config_env_file), key_env)
    if not value:
        raise ValueError(f"Missing environment variable {key_env}")
    return value


def checked_mimo_reply(response: httpx.Response, purpose: str) -> dict:
    """Keep provider error bodies, audio and credentials out of exceptions."""
    if not response.is_success:
        raise RuntimeError(f"MiMo {purpose} failed (HTTP {response.status_code})")
    try:
        body = response.json()
        if not isinstance(body, dict) or not body.get("choices"):
            raise ValueError
        return body
    except (ValueError, TypeError):
        raise RuntimeError(f"MiMo {purpose} returned an invalid response") from None


def pcm_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Encode mono PCM16 as a WAV payload, without writing temporary audio."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buffer.getvalue()


def synthesize_mimo(text: str, config: MiMoTTSConfig) -> AudioData:
    """Synthesize the exact supplied text and normalize to tau's PCM16 16 kHz."""
    key = mimo_api_key(config.api_key_env, config.config_env_file)
    try:
        with httpx.Client(timeout=config.request_timeout_seconds) as client:
            response = client.post(
                config.base_url.rstrip("/") + "/chat/completions",
                headers={"api-key": key},
                json={
                    "model": config.model_id,
                    "messages": [{"role": "assistant", "content": text}],
                    "audio": {"format": "wav", "voice": config.voice_id},
                    "stream": False,
                },
            )
    except httpx.HTTPError:
        # Preserve retry semantics without exposing provider URLs or response
        # details in logs.  ``tts_retry`` recognizes ConnectionError.
        raise MiMoTTSConnectionError("MiMo TTS connection failed") from None
    body = checked_mimo_reply(response, "TTS")
    try:
        encoded = body["choices"][0]["message"]["audio"]["data"]
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw)) as reader:
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                raise ValueError("Expected uncompressed PCM16")
            channels, rate = reader.getnchannels(), reader.getframerate()
            data = reader.readframes(reader.getnframes())
        if channels == 2:
            data = audioop.tomono(data, 2, 0.5, 0.5)
        elif channels != 1:
            raise ValueError("Expected mono or stereo")
        if rate != DEFAULT_PCM_SAMPLE_RATE:
            data, _ = audioop.ratecv(data, 2, 1, rate, DEFAULT_PCM_SAMPLE_RATE, None)
        if not data:
            raise ValueError("Empty audio")
    except (KeyError, IndexError, TypeError, ValueError, wave.Error, EOFError):
        raise RuntimeError("MiMo TTS returned invalid PCM16 WAV audio") from None
    return AudioData(
        data=data,
        format=AudioFormat(
            encoding=AudioEncoding.PCM_S16LE, sample_rate=DEFAULT_PCM_SAMPLE_RATE
        ),
    )


async def transcribe_mimo(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    key: str,
    pcm: bytes,
    sample_rate: int,
    language: str,
) -> str:
    """Recognize one already-played segment; API provides no word timestamps."""
    if sample_rate != DEFAULT_MIMO_ASR_SAMPLE_RATE:
        pcm, _ = audioop.ratecv(
            pcm, 2, 1, sample_rate, DEFAULT_MIMO_ASR_SAMPLE_RATE, None
        )
        sample_rate = DEFAULT_MIMO_ASR_SAMPLE_RATE
    try:
        response = await client.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"api-key": key},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": base64.b64encode(
                                        pcm_wav(pcm, sample_rate)
                                    ).decode(),
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": language},
                "stream": False,
            },
        )
    except httpx.HTTPError:
        raise RuntimeError("MiMo ASR connection failed") from None
    body = checked_mimo_reply(response, "ASR")
    try:
        text = body["choices"][0]["message"]["content"]
        if not isinstance(text, str):
            raise ValueError
        return text.strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("MiMo ASR returned an invalid transcript") from None

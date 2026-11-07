"""DashScope file ASR with private credentials and optional weighted vocabulary."""

import audioop
import base64
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, Field

from tau2.config import DEFAULT_QWEN_ASR_SAMPLE_RATE
from tau2.voice.mimo import pcm_wav


class QwenASRResult(BaseModel):
    """Only the transcript and numeric provider usage cross the API boundary."""

    text: str
    usage: dict[str, int | float] = Field(default_factory=dict)


def qwen_asr_url(base_url: str) -> str:
    """Use the configured DashScope region for its synchronous HTTP ASR API."""
    parsed = urlsplit(base_url)
    schemes = {"wss": "https", "ws": "http", "https": "https", "http": "http"}
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid Qwen ASR base URL")
    path = parsed.path.rstrip("/")
    endpoint = "/api/v1/services/aigc/multimodal-generation/generation"
    if not path.endswith(endpoint):
        for suffix in ("/api-ws/v1/realtime", "/compatible-mode/v1", "/api/v1"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        else:
            if path:
                raise ValueError("Unsupported Qwen ASR base URL path")
        path += endpoint
    return urlunsplit((schemes[parsed.scheme], parsed.netloc, path, "", ""))


async def transcribe_qwen(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    key: str,
    pcm: bytes,
    sample_rate: int,
    language: str,
    vocabulary: dict[str, int] | None = None,
) -> QwenASRResult:
    """Recognize actual PCM playback without exposing provider error bodies."""
    if sample_rate != DEFAULT_QWEN_ASR_SAMPLE_RATE:
        pcm, _ = audioop.ratecv(
            pcm, 2, 1, sample_rate, DEFAULT_QWEN_ASR_SAMPLE_RATE, None
        )
    parameters = {"format": "wav", "sample_rate": str(DEFAULT_QWEN_ASR_SAMPLE_RATE)}
    if language and language != "auto":
        parameters["language_hints"] = [language]
    if vocabulary:
        if len(vocabulary) > 2000:
            raise ValueError("Qwen ASR vocabulary exceeds 2000 phrases")
        parameters["vocabulary"] = dict(vocabulary)
    payload = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": "data:audio/wav;base64,"
                                + base64.b64encode(
                                    pcm_wav(pcm, DEFAULT_QWEN_ASR_SAMPLE_RATE)
                                ).decode(),
                            },
                        }
                    ],
                }
            ]
        },
        "parameters": parameters,
    }
    try:
        response = await client.post(
            qwen_asr_url(base_url),
            json=payload,
            headers={"Authorization": "Bearer " + key, "X-DashScope-SSE": "disable"},
        )
    except httpx.HTTPError:
        raise RuntimeError("Qwen ASR connection failed") from None
    if not response.is_success:
        raise RuntimeError(f"Qwen ASR failed (HTTP {response.status_code})")
    try:
        body = response.json()
        text = body["output"]["text"]
        if not isinstance(text, str):
            raise ValueError
        usage = body.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError
        return QwenASRResult(
            text=text.strip(),
            usage={
                k: v
                for k, v in usage.items()
                if k in {"duration", "input_tokens", "output_tokens", "total_tokens"}
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
            },
        )
    except (ValueError, KeyError, TypeError, IndexError):
        raise RuntimeError("Qwen ASR returned an invalid response") from None

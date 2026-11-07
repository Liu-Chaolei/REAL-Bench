"""Local audio endpoints for independent ASR and MiMo TTS providers."""

import asyncio
import audioop
import base64
import io
import secrets
import wave
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response


def wav_pcm(data: bytes, target_rate: int = 24000) -> bytes:
    """Validate a bounded PCM16 WAV and normalize mono sample rate."""
    try:
        with wave.open(io.BytesIO(data)) as reader:
            if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
                raise ValueError
            channels, rate = reader.getnchannels(), reader.getframerate()
            if channels not in (1, 2) or not 8000 <= rate <= 192000:
                raise ValueError
            if reader.getnframes() > rate * 180:
                raise ValueError
            pcm = reader.readframes(reader.getnframes())
        if channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        if rate != target_rate:
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, target_rate, None)
        if not pcm or len(pcm) % 2:
            raise ValueError
        return pcm
    except (ValueError, wave.Error, EOFError, audioop.error):
        raise HTTPException(
            422, "Expected nonempty PCM16 WAV, at most 180 seconds"
        ) from None


def pcm_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """Encode mono PCM at the selected provider's rate without temporary files."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return stream.getvalue()


async def until_disconnected(request: Request, operation):
    """Cancel provider HTTP work when the native speech handler cancels its upload."""

    async def disconnected():
        # The body has already been consumed. Await ASGI disconnect directly:
        # Request.is_disconnected's internal CancelScope can swallow Task.cancel.
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.create_task(operation)
    monitor = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait(
            {work, monitor}, return_when=asyncio.FIRST_COMPLETED
        )
        if work in done:
            return await work
        raise HTTPException(499, "Audio request cancelled")
    finally:
        work.cancel()
        monitor.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)


def dashscope_asr_url(base_url: str) -> str:
    """Resolve a regional DashScope HTTP endpoint from its configured base URL."""
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
        raise ValueError("Invalid DashScope ASR base URL")
    path = parsed.path.rstrip("/")
    endpoint = "/api/v1/services/aigc/multimodal-generation/generation"
    if not path.endswith(endpoint):
        for suffix in ("/api-ws/v1/realtime", "/compatible-mode/v1", "/api/v1"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        else:
            if path:
                raise ValueError("Unsupported DashScope ASR base URL path")
        path += endpoint
    return urlunsplit((schemes[parsed.scheme], parsed.netloc, path, "", ""))


def create_app(
    settings: dict,
    key: str,
    token: str,
    transport=None,
    *,
    asr_key: str | None = None,
    asr_base_url: str | None = None,
) -> FastAPI:
    """Build only the two native audio endpoints; never proxy arbitrary URLs."""
    qwen_asr = settings["asr_model"] == "qwen-audio-3.1-asr-flash"
    qwen_url = None
    if qwen_asr:
        if not asr_key or not asr_base_url:
            raise ValueError("Qwen frontend ASR requires its own URL and credential")
        qwen_url = dashscope_asr_url(asr_base_url)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=settings["request_timeout_seconds"], transport=transport
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def call(request, payload, *, use_qwen=False):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + token):
            raise HTTPException(403, "Local audio authorization required")

        provider = "Qwen ASR" if use_qwen else "MiMo audio"

        async def post():
            try:
                response = await app.state.client.post(
                    qwen_url
                    if use_qwen
                    else settings["mimo_base_url"].rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + asr_key,
                        "X-DashScope-SSE": "disable",
                    }
                    if use_qwen
                    else {"api-key": key},
                    json=payload,
                )
            except httpx.TimeoutException:
                raise HTTPException(504, f"{provider} request timed out") from None
            except httpx.HTTPError:
                raise HTTPException(502, f"{provider} connection failed") from None
            if not response.is_success:
                raise HTTPException(
                    502, f"{provider} returned HTTP {response.status_code}"
                )
            try:
                body = response.json()
                if use_qwen:
                    text = body["output"]["text"]
                    if not isinstance(text, str):
                        raise ValueError
                    return {"content": text}
                return body["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise HTTPException(
                    502, f"{provider} returned an invalid response"
                ) from None

        return await until_disconnected(request, post())

    @app.get("/health")
    async def health():
        return {"ready": True}

    @app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request):
        async with request.form(
            max_files=1, max_fields=8, max_part_size=12000000
        ) as form:
            file = form.get("file")
            if not file or not hasattr(file, "read"):
                raise HTTPException(422, "Audio file is required")
            if form.get("model") != settings["asr_model"]:
                raise HTTPException(422, "Unexpected ASR model")
            if form.get("response_format", "json") != "json":
                raise HTTPException(422, "Only JSON transcription is supported")
            data = await file.read(12000001)
            if len(data) > 12000000:
                raise HTTPException(413, "Audio upload is too large")
            rate = 16000 if qwen_asr else 24000
            wav = pcm_wav(wav_pcm(data, target_rate=rate), sample_rate=rate)
            language = form.get("language") or settings["language"]
        if qwen_asr:
            parameters = {"format": "wav", "sample_rate": str(rate)}
            if language and language != "auto":
                parameters["language_hints"] = [language]
            payload = {
                "model": settings["asr_model"],
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": "data:audio/wav;base64,"
                                        + base64.b64encode(wav).decode(),
                                    },
                                }
                            ],
                        }
                    ]
                },
                "parameters": parameters,
            }
        else:
            payload = {
                "model": settings["asr_model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": base64.b64encode(wav).decode(),
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": language},
                "stream": False,
            }
        message = await call(request, payload, use_qwen=qwen_asr)
        text = message.get("content")
        if not isinstance(text, str):
            raise HTTPException(502, "MiMo ASR returned an invalid transcript")
        return JSONResponse({"text": text.strip(), "language": language})

    @app.post("/v1/audio/speech")
    async def synthesize(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "Expected JSON speech request") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "Expected JSON speech request")
        text = body.get("input")
        voice = body.get("voice", settings["voice"])
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise HTTPException(
                422, "Expected nonempty speech text, at most 16000 characters"
            )
        if body.get("model") != settings["tts_model"] or voice != settings["voice"]:
            raise HTTPException(422, "Unexpected TTS model or voice")
        if body.get("response_format") != "pcm" or body.get("stream", False):
            raise HTTPException(422, "Expected non-streaming PCM speech request")
        if body.get("speed", 1) != 1 or body.get("instructions"):
            raise HTTPException(422, "Speech speed and style overrides are unsupported")
        message = await call(
            request,
            {
                "model": settings["tts_model"],
                "messages": [{"role": "assistant", "content": text}],
                "audio": {"format": "wav", "voice": voice},
                "stream": False,
            },
        )
        try:
            data = base64.b64decode(message["audio"]["data"], validate=True)
            pcm = wav_pcm(data)
        except (KeyError, TypeError, ValueError, HTTPException):
            raise HTTPException(502, "MiMo TTS returned invalid WAV audio") from None
        return Response(pcm, media_type="audio/pcm")

    return app

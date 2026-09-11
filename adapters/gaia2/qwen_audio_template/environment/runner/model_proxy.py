"""Credential-owning Responses proxy with an auditable, fixed model profile."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def make_app(endpoint, key, model, effort, token, output, *, transport=None):
    dispatch = asyncio.Event()
    dispatch.set()
    active = 0
    drained = asyncio.Event()
    drained.set()
    sequence = 0
    sealed = False
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    def failure(cause, status=None):
        with (output / "provider-failures.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "component": "backend_provider",
                        "cause": cause,
                        "http_status": status,
                        "wall_time": time.time(),
                    }
                )
                + "\n"
            )

    async def seal(request: Request):
        nonlocal sealed
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        sealed = True
        # Wake queued admissions so they can reject without touching evidence.
        dispatch.set()
        await drained.wait()
        return JSONResponse({"sealed": True})

    async def barrier(request: Request):
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        paused = (await request.json()).get("paused")
        if not isinstance(paused, bool):
            return JSONResponse({"error": "Boolean paused required"}, status_code=400)
        if sealed:
            return JSONResponse({"error": "Trial sealed"}, status_code=409)
        if paused:
            dispatch.clear()
            await drained.wait()
        else:
            dispatch.set()
        return JSONResponse({"ok": True})

    async def state(request):
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse({"active": active, "sequence": sequence, "sealed": sealed})

    async def models(request):
        return JSONResponse(
            {"object": "list", "data": [{"id": model, "object": "model"}]}
        )

    async def responses(request: Request):
        nonlocal active, sequence
        body = await request.json()
        if sealed:
            return JSONResponse({"error": "Trial sealed"}, status_code=409)
        if (
            body.get("model") != model
            or body.get("reasoning", {}).get("effort") != effort
        ):
            failure("adapter_model_profile", 400)
            return JSONResponse(
                {"error": "Model and reasoning effort must match the frozen profile"},
                status_code=400,
            )
        await dispatch.wait()
        if sealed:
            return JSONResponse({"error": "Trial sealed"}, status_code=409)
        # No await between acquiring admission and active accounting.
        active += 1
        drained.clear()
        sequence += 1
        stem = output / f"request-{sequence:06d}"
        stem.with_suffix(".json").write_text(json.dumps(body, ensure_ascii=False))
        started = time.monotonic()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=30), trust_env=False, transport=transport
        )
        try:
            response = await client.send(
                client.build_request(
                    "POST",
                    endpoint.rstrip("/") + "/responses",
                    json=body,
                    headers={"Authorization": f"Bearer {key}"},
                ),
                stream=True,
            )
        except Exception:
            failure("provider_connection_failure")
            active -= 1
            if not active:
                drained.set()
            await client.aclose()
            raise

        if (
            response.status_code in {401, 402, 403, 404, 429}
            or response.status_code >= 500
        ):
            failure(
                "provider_rate_limit"
                if response.status_code == 429
                else "provider_unavailable",
                response.status_code,
            )

        async def stream():
            nonlocal active
            raw = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    yield chunk
            except Exception:
                failure("provider_stream_failure")
                raise
            finally:
                await response.aclose()
                await client.aclose()
                stem.with_suffix(".response").write_bytes(raw)
                usage = None
                try:
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        for line in raw.decode().splitlines():
                            if (
                                line.startswith("data:")
                                and line[5:].strip() != "[DONE]"
                            ):
                                item = json.loads(line[5:])
                                if item.get("type") in {"error", "response.failed"}:
                                    error = (
                                        item.get("error")
                                        or item.get("response", {}).get("error")
                                        or item
                                    )
                                    code = str(error.get("code", ""))
                                    if code in {
                                        "rate_limit_exceeded",
                                        "insufficient_quota",
                                        "invalid_api_key",
                                        "server_error",
                                        "billing_hard_limit_reached",
                                    }:
                                        failure(
                                            "provider_unavailable", response.status_code
                                        )
                                if item.get("type") == "response.completed":
                                    usage = item.get("response", {}).get("usage")
                    else:
                        usage = json.loads(raw).get("usage")
                except (ValueError, UnicodeError):
                    pass
                stem.with_suffix(".metadata.json").write_text(
                    json.dumps(
                        {
                            "model": body["model"],
                            "usage": usage,
                            "reasoning_effort": effort,
                            "api": "responses",
                            "status": response.status_code,
                            "duration_sec": time.monotonic() - started,
                            "request_sha256": hashlib.sha256(
                                stem.with_suffix(".json").read_bytes()
                            ).hexdigest(),
                            "response_sha256": hashlib.sha256(raw).hexdigest(),
                        }
                    )
                )
                active -= 1
                if not active:
                    drained.set()

        return StreamingResponse(
            stream(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return Starlette(
        routes=[
            Route("/v1/responses", responses, methods=["POST"]),
            Route("/v1/models", models),
            Route("/__barrier", barrier, methods=["POST"]),
            Route("/__seal", seal, methods=["POST"]),
            Route("/__state", state),
        ]
    )


if __name__ == "__main__":
    uvicorn.run(
        make_app(
            os.environ["GAIA2_BACKEND_ENDPOINT"],
            os.environ["GAIA2_BACKEND_API_KEY"],
            os.environ["GAIA2_BACKEND_MODEL"],
            os.environ["GAIA2_BACKEND_REASONING_EFFORT"],
            os.environ["QWAUDIO_EVALUATION_TOKEN"],
            "/evaluation/results/model-requests",
        ),
        host="0.0.0.0",
        port=8001,
    )

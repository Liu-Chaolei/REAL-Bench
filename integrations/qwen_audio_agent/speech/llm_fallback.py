"""Forward native Responses requests to one of two fixed frontend endpoints."""

import asyncio
import json
import logging
import secrets

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mimo_gateway import until_disconnected

logger = logging.getLogger(__name__)


def install_llm_fallback(app, token: str, endpoints: list[tuple[str, str]]) -> None:
    """Switch a call to its backup after failure, without replaying emitted output.

    Initial SSE metadata stays buffered until usable output arrives. This permits
    fallback on HTTP-200 error events as well as HTTP and connection failures.
    Once content or tool arguments leave this proxy, this request is never retried.
    The next request uses the backup if the primary failed partway through.
    """
    active = 0

    def failed(index):
        nonlocal active
        if index == 0 and active == 0:
            active = 1
            logger.warning("Frontend LLM fallback activated; using secondary endpoint")

    @app.post("/v1/responses")
    async def responses(request: Request):
        if not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + token
        ):
            raise HTTPException(403, "Local LLM authorization required")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(422, "Expected a Responses request")

        if not payload.get("stream", False):

            async def complete():
                # Native startup warmup and history compaction are non-streaming.
                for index in range(active, len(endpoints)):
                    url, key = endpoints[index]
                    try:
                        upstream = await app.state.client.post(
                            url.rstrip("/") + "/responses",
                            headers={"Authorization": "Bearer " + key},
                            json=payload,
                        )
                        if not upstream.is_success:
                            raise ValueError("Upstream HTTP error")
                        value = upstream.json()
                        if (
                            not isinstance(value, dict)
                            or value.get("error")
                            or value.get("status") == "failed"
                            or (value.get("base_resp") or {}).get("status_code", 0)
                        ):
                            raise ValueError("Upstream response failed")
                        return JSONResponse(value)
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        failed(index)
                raise HTTPException(502, "Frontend LLM request failed")

            return await until_disconnected(request, complete())

        async def stream():
            for index in range(active, len(endpoints)):
                url, key = endpoints[index]
                emitted = False
                pending = []
                terminal = False
                try:
                    async with app.state.client.stream(
                        "POST",
                        url.rstrip("/") + "/responses",
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                    ) as upstream:
                        if not upstream.is_success:
                            raise ValueError("Upstream HTTP error")
                        data = []
                        async for line in upstream.aiter_lines():
                            if line:
                                if line.startswith("data:"):
                                    data.append(line[5:].lstrip())
                                continue
                            if not data:
                                continue
                            raw = "\n".join(data)
                            data = []
                            if raw == "[DONE]":
                                continue
                            event = json.loads(raw)
                            kind = event.get("type", "")
                            if (
                                kind in {"error", "response.failed"}
                                or event.get("error")
                                or (event.get("base_resp") or {}).get("status_code", 0)
                            ):
                                raise ValueError("Upstream error event")
                            block = "data: " + raw + "\n\n"
                            # Metadata alone must not prevent quota/auth fallback.
                            usable = (
                                bool(event.get("delta"))
                                or kind
                                in {
                                    "response.function_call_arguments.done",
                                    "response.output_item.done",
                                    "response.completed",
                                    "response.incomplete",
                                }
                                or (event.get("item") or {}).get("type")
                                == "function_call"
                            )
                            if emitted or usable:
                                for initial in pending:
                                    yield initial
                                pending.clear()
                                emitted = True
                                yield block
                            else:
                                pending.append(block)
                            if kind in {"response.completed", "response.incomplete"}:
                                terminal = True
                        if not terminal:
                            raise ValueError("Upstream stream ended before completion")
                    return
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    failed(index)
                    if not emitted and index + 1 < len(endpoints):
                        continue
                    # Never expose provider bodies, keys or URLs in native errors.
                    yield (
                        'data: {"type":"error","error":'
                        '{"code":"frontend_llm_unavailable",'
                        '"message":"Frontend LLM request failed"}}\n\n'
                    )
                    return

        # StreamingResponse cancels the upstream iterator on client disconnect.
        return StreamingResponse(stream(), media_type="text/event-stream")

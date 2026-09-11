"""Responses transport for the official ARE judge's unchanged checker prompts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx


class ResponsesJudge:
    def __init__(
        self,
        model: str,
        endpoint: str,
        api_key: str,
        effort: str = "low",
        *,
        client: httpx.Client | None = None,
        record_path: Path | None = None,
        replay_path: Path | None = None,
    ) -> None:
        self.model = model
        self.endpoint = endpoint.rstrip("/") + "/responses"
        self.api_key = api_key
        self.effort = effort
        self.client = client or httpx.Client(timeout=120)
        self.calls = 0
        self.record_path = record_path
        self.replay = (
            [json.loads(line) for line in replay_path.read_text().splitlines()]
            if replay_path
            else None
        )

    def close(self) -> None:
        self.client.close()

    def __call__(
        self,
        messages: list[dict[str, Any]],
        stop_sequences: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any] | None]:
        payload = {
            "model": self.model,
            "input": messages,
            "reasoning": {"effort": self.effort},
            "max_output_tokens": 4096,
            "store": False,
            "stream": True,
        }
        self.calls += 1
        input_hash = hashlib.sha256(
            json.dumps(
                {"payload": payload, "stops": stop_sequences}, sort_keys=True
            ).encode()
        ).hexdigest()
        if self.replay is not None:
            if self.calls > len(self.replay):
                raise RuntimeError("Judge replay exhausted")
            row = self.replay[self.calls - 1]
            if row["input_sha256"] != input_hash:
                raise RuntimeError("Judge replay input differs from recorded request")
            return row["text"], row["usage"]
        chunks = []
        completed: dict[str, Any] | None = None
        with self.client.stream(
            "POST",
            self.endpoint,
            json=payload,
            headers={"Authorization": "Bearer " + self.api_key},
        ) as response:
            if not response.is_success:
                message = response.read().decode(errors="replace")
                message = (
                    message.replace(self.api_key, "[redacted]")
                    if self.api_key
                    else message
                )
                raise RuntimeError(
                    f"Judge Responses HTTP {response.status_code}: {message[:500]}"
                )
            if "text/event-stream" not in response.headers.get("content-type", ""):
                data = json.loads(response.read())
                if data.get("status") != "completed":
                    raise RuntimeError("Judge Responses request did not complete")
                completed = data
            else:
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        chunks.append(event.get("delta", ""))
                    elif kind == "response.completed":
                        completed = event["response"]
                    elif kind in {"error", "response.failed", "response.incomplete"}:
                        # Keep credentials and provider-specific response bodies out of logs.
                        raise RuntimeError(f"Judge Responses stream ended with {kind}")
        if completed is None:
            raise RuntimeError(
                "Judge Responses stream closed without a completion event"
            )
        text = "".join(
            part.get("text", "")
            for item in completed.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ) or "".join(chunks)
        if not text.strip():
            raise RuntimeError("Judge Responses returned no text")
        # ARE LLMChecker matches [[True]]/[[False]] case-sensitively.
        # Preserve provider text; lowercasing valid verdicts silently rejects them.
        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"), ""
        )
        for yes, no in [("[[True]]", "[[False]]"), ("[[Success]]", "[[Failure]]")]:
            if yes in system and no in system and yes not in text and no not in text:
                raise RuntimeError("Judge returned an unparseable verdict")
        for stop in stop_sequences:
            text = text.split(stop)[0]
        if self.record_path:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "input_sha256": input_hash,
                            "payload": payload,
                            "text": text,
                            "usage": completed.get("usage"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return text, completed.get("usage")

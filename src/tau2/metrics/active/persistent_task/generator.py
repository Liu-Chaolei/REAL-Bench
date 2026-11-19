"""LLM request generation with validated language-template fallback."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tau2.metrics.active.config import RequestGeneratorConfig
from tau2.metrics.active.persistent_task.policy import ResolvedIntent


@dataclass(frozen=True)
class RequestGenerationContext:
    intent: ResolvedIntent
    language: str
    task_label: str | None
    seed: int


@dataclass(frozen=True)
class GeneratedRequest:
    text: str
    generator: str
    language: str
    state: str
    operation: str
    fallback_reason: str | None = None


class RequestGenerator:
    """Generate one request and fall back to a versioned language catalog."""

    def __init__(self, config: RequestGeneratorConfig, template_root: Path):
        self.config = config
        self.template_root = template_root

    def generate(self, context: RequestGenerationContext) -> GeneratedRequest:
        error: Exception | None = None
        for _ in range(self.config.max_attempts):
            try:
                executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="active-metric-llm"
                )
                future = executor.submit(self._generate_llm, context)
                try:
                    value = future.result(timeout=self.config.timeout_seconds)
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
                self._validate(value, context)
                return GeneratedRequest(
                    text=value["text"].strip(),
                    generator="llm",
                    language=context.language,
                    state=context.intent.state.value,
                    operation=context.intent.operation,
                )
            except Exception as exc:
                error = exc
        fallback = self._template(context)
        if fallback is None:
            raise RuntimeError("generation_unavailable") from error
        return GeneratedRequest(
            text=fallback,
            generator="template-fallback",
            language=context.language,
            state=context.intent.state.value,
            operation=context.intent.operation,
            fallback_reason=type(error).__name__ if error else "llm_disabled",
        )

    def _generate_llm(self, context: RequestGenerationContext) -> dict[str, Any]:
        from tau2.data_model.message import UserMessage
        from tau2.utils.llm_utils import generate

        prompt = (
            "Generate one concise user request for a customer-service agent. "
            "Return JSON with text, language, state and operation. "
            "Do not answer the request. Do not mention internal IDs. "
            f"Language: {context.language}. State: {context.intent.state.value}. "
            f"Operation: {context.intent.operation}. "
            f"Reference style: {context.intent.reference_mode}. "
            f"Task label: {context.task_label or 'that task'}."
        )
        model = self.config.model.model
        if self.config.model_env:
            model = os.environ.get(self.config.model_env, model)
        if "/" not in model:
            model = "openai/" + model
        args = self.config.model.arguments()
        if self.config.api_base_env:
            args["api_base"] = os.environ[self.config.api_base_env]
        if self.config.api_key_env:
            args["api_key"] = os.environ[self.config.api_key_env]
        response = generate(
            messages=[UserMessage(role="user", content=prompt)],
            model=model,
            **args,
        )
        content = response if isinstance(response, str) else getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(str(item) for item in content)
        if not isinstance(content, str):
            raise ValueError("invalid_generator_response")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise ValueError("generator_response_is_not_json")
            return json.loads(match.group(0))

    def _validate(self, value: dict[str, Any], context: RequestGenerationContext) -> None:
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty_generated_request")
        if len(text) > self.config.max_characters:
            raise ValueError("generated_request_too_long")
        if value.get("language") != context.language:
            raise ValueError("generated_request_language_mismatch")
        if value.get("state") != context.intent.state.value:
            raise ValueError("generated_request_state_mismatch")
        if value.get("operation") != context.intent.operation:
            raise ValueError("generated_request_operation_mismatch")
        forbidden = ("backend_task_id", "request_id", "source_task_id")
        if any(token in text for token in forbidden):
            raise ValueError("generated_request_leaks_identity")
        if context.intent.state.value in {"running", "waiting", "queued"} and re.search(
            r"\b(already\s+)?completed|finished\s+successfully|result\s+is",
            text,
            re.IGNORECASE,
        ):
            raise ValueError("generated_request_claims_completion")

    def _template(self, context: RequestGenerationContext) -> str | None:
        path = self.template_root / f"{context.language}.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text())
        key = "/".join(
            [
                context.intent.state.value,
                context.intent.operation,
                context.intent.reference_mode,
            ]
        )
        values = payload.get("templates", {}).get(key, [])
        if not values:
            return None
        index = context.seed % len(values)
        text = str(values[index]).replace("{task_label}", context.task_label or "that task")
        if len(text) > self.config.max_characters:
            return None
        return text


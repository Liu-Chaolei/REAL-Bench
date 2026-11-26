"""Cached, auditable model requests through tau's existing provider abstraction."""

import asyncio
import json
import logging
import threading
from pathlib import Path

from tau2.diagnostics.intent.io import canonical, digest, write_json

logger = logging.getLogger(__name__)


def _is_timeout_error(error: BaseException) -> bool:
    """Recognize timeout wrappers without retrying unrelated provider errors."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, asyncio.TimeoutError)):
            return True
        name = type(current).__name__.lower()
        if "timeout" in name or "timedout" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


class RequestBudget:
    """Shared request cap for one trial, including malformed-response repair."""

    def __init__(self, maximum):
        self.maximum, self.used = maximum, 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            if self.used >= self.maximum:
                raise RuntimeError("Diagnostic request budget exhausted")
            self.used += 1


class TauJsonModel:
    """Fresh model history per request with content-addressed successful replay."""

    TIMEOUT_RETRIES = 1
    OUTER_TIMEOUT_GRACE_SECONDS = 5.0

    def __init__(self, config, role, directory: Path, budget):
        self.config, self.role, self.directory, self.budget = (
            config,
            role,
            directory,
            budget,
        )

    def cache_path(self, prompt, schema):
        """Return the content-addressed cache path for one request."""
        profile = self.config.profile(self.role)
        identity = digest(
            {
                "prompt": prompt,
                "schema": schema,
                "profile": profile.model_dump(),
                "implementation": "tau-json/2",
            }
        )
        return self.directory / (identity + ".json")

    def cache_exists(self, prompt, schema):
        """Whether a valid cached response file exists for one request."""
        return self.cache_path(prompt, schema).is_file()

    def _complete_once(self, prompt, schema, *, validate=None):
        from tau2.data_model.message import UserMessage
        from tau2.utils.llm_utils import extract_json_from_llm_response, generate

        if len(prompt) > self.config.max_characters:
            raise ValueError("diagnostic_input_too_long")
        profile = self.config.profile(self.role)
        path = self.cache_path(prompt, schema)
        identity = path.stem
        if path.exists():
            row = json.loads(path.read_text())
            if row.get("identity") == identity and row.get("output_sha256") == digest(
                row.get("output")
            ):
                if validate is not None:
                    validate(row["output"])
                return row["output"]
        args = profile.arguments()
        args.setdefault("timeout", self.config.timeout_seconds)
        if self.role in {
            "interaction",
            "state_interaction",
            "feedback_nuggets",
            "feedback_alignment",
        }:
            args.update(num_retries=0, response_validation_retries=0)
        content = (
            prompt + "\nOutput JSON schema:\n" + canonical(schema) if schema else prompt
        )
        attempt_path, attempt = None, None
        if self.role in {"feedback_nuggets", "feedback_alignment"}:
            from uuid import uuid4

            if len(content) > self.config.max_characters:
                raise ValueError("diagnostic_input_too_long")
            attempt_path = (
                self.directory / "attempts" / (identity + "_" + uuid4().hex + ".json")
            )
            attempt = {
                "identity": identity,
                "role": self.role,
                "prompt": prompt,
                "schema": schema,
                "profile": profile.model_dump(),
                "status": "started",
            }
        self.budget.take()
        if attempt_path:
            write_json(attempt_path, attempt)
        try:
            response = generate(
                model=profile.model,
                messages=[UserMessage(role="user", content=content)],
                call_name="diagnostic_" + self.role,
                response_format={"type": "json_object"},
                **args,
            )
        except Exception as exc:
            if attempt_path:
                write_json(
                    attempt_path,
                    attempt
                    | {"status": "provider_error", "reason": type(exc).__name__},
                )
            raise
        if attempt_path:
            # Preserve malformed or semantically invalid responses before parsing.
            write_json(
                attempt_path,
                attempt
                | {
                    "status": "responded",
                    "raw_response": response.content,
                    "usage": response.usage,
                    "cost": response.cost,
                },
            )
        raw = extract_json_from_llm_response(response.content or "")
        json.loads(raw)
        if validate is not None:
            validate(raw)
        write_json(
            path,
            {
                "identity": identity,
                "role": self.role,
                "prompt": prompt,
                "schema": schema,
                "profile": profile.model_dump(),
                "output": raw,
                "output_sha256": digest(raw),
                "usage": response.usage,
                "cost": response.cost,
            },
        )
        return raw

    def complete_sync(self, prompt, schema, *, validate=None):
        """Run one diagnostic request, retrying a provider timeout once."""
        attempts = 1 if self.role == "state_interaction" else self.TIMEOUT_RETRIES + 1
        for attempt in range(attempts):
            try:
                return self._complete_once(prompt, schema, validate=validate)
            except Exception as exc:
                if attempt >= attempts - 1 or not _is_timeout_error(exc):
                    raise
                logger.warning(
                    "Diagnostic %s request timed out; retrying once (%d/%d)",
                    self.role,
                    attempt + 1,
                    self.TIMEOUT_RETRIES,
                )

    async def complete(self, prompt, schema):
        """Run with an outer deadline and retry one provider timeout."""
        timeout = (
            self.config.profile(self.role).args.get(
                "timeout", self.config.timeout_seconds
            )
            + self.OUTER_TIMEOUT_GRACE_SECONDS
        )
        for attempt in range(self.TIMEOUT_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._complete_once, prompt, schema),
                    timeout=timeout,
                )
            except Exception as exc:
                if attempt >= self.TIMEOUT_RETRIES or not _is_timeout_error(exc):
                    raise
                logger.warning(
                    "Diagnostic %s request exceeded %.1fs; retrying once (%d/%d)",
                    self.role,
                    timeout,
                    attempt + 1,
                    self.TIMEOUT_RETRIES,
                )

    def judge(self, prompt):
        value = json.loads(
            self.complete_sync(
                prompt,
                {
                    "type": "object",
                    "properties": {
                        "passed": {"type": ["boolean", "null"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["passed", "reason"],
                },
            )
        )
        if value.get("passed") is not None and type(value["passed"]) is not bool:
            raise ValueError("Diagnostic judge requires boolean or null")
        return value

    def text_engine(self, messages, **kwargs):
        """Preserve native checker prompts and their exact response parser."""
        from tau2.data_model.message import AssistantMessage, SystemMessage, UserMessage
        from tau2.utils.llm_utils import generate

        profile = self.config.profile(self.role)
        identity = digest(
            {
                "messages": messages,
                "profile": profile.model_dump(),
                "implementation": "tau-native-are/1",
            }
        )
        path = self.directory / (identity + ".json")
        if path.exists():
            row = json.loads(path.read_text())
            if row.get("identity") == identity and row.get("output_sha256") == digest(
                row.get("output")
            ):
                return row["output"], None
        if len(canonical(messages)) > self.config.max_characters:
            raise ValueError("diagnostic_input_too_long")
        self.budget.take()
        types = {
            "system": SystemMessage,
            "user": UserMessage,
            "assistant": AssistantMessage,
        }
        args = profile.arguments()
        args.setdefault("timeout", self.config.timeout_seconds)
        response = generate(
            model=profile.model,
            messages=[types[m["role"]](**m) for m in messages],
            call_name="diagnostic_native_execution",
            **args,
        )
        output = response.content
        if not isinstance(output, str) or not output.strip():
            raise ValueError("empty_native_checker_response")
        write_json(
            path,
            {
                "identity": identity,
                "messages": messages,
                "profile": profile.model_dump(),
                "output": output,
                "output_sha256": digest(output),
                "usage": response.usage,
                "cost": response.cost,
            },
        )
        return output, None

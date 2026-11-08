"""Cached, auditable model requests through tau's existing provider abstraction."""

import asyncio
import json
import threading
from pathlib import Path

from tau2.diagnostics.intent.io import canonical, digest, write_json


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

    def __init__(self, config, role, directory: Path, budget):
        self.config, self.role, self.directory, self.budget = (
            config,
            role,
            directory,
            budget,
        )

    def complete_sync(self, prompt, schema, *, validate=None):
        from tau2.data_model.message import UserMessage
        from tau2.utils.llm_utils import extract_json_from_llm_response, generate

        if len(prompt) > self.config.max_characters:
            raise ValueError("diagnostic_input_too_long")
        profile = self.config.profile(self.role)
        identity = digest(
            {
                "prompt": prompt,
                "schema": schema,
                "profile": profile.model_dump(),
                "implementation": "tau-json/1",
            }
        )
        path = self.directory / (identity + ".json")
        if path.exists():
            row = json.loads(path.read_text())
            if row.get("identity") == identity and row.get("output_sha256") == digest(
                row.get("output")
            ):
                if validate is not None:
                    validate(row["output"])
                return row["output"]
        args = profile.arguments()
        if self.role in {"interaction", "feedback_nuggets", "feedback_alignment"}:
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
                timeout=self.config.timeout_seconds,
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

    async def complete(self, prompt, schema):
        return await asyncio.to_thread(self.complete_sync, prompt, schema)

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
        response = generate(
            model=profile.model,
            messages=[types[m["role"]](**m) for m in messages],
            call_name="diagnostic_native_execution",
            timeout=self.config.timeout_seconds,
            **profile.arguments(),
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

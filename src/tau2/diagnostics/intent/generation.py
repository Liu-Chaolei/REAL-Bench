"""Common mechanics for bounded, auditable JSON generation and repair."""

import json
import re
from typing import Any, Callable

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.config import ModelConfig, ModelConfigurationError
from tau2.diagnostics.intent.io import canonical, digest, escape
from tau2.diagnostics.intent.types import JsonModel

GENERATION_VERSION = "tau-json/2"
VALIDATION_ERROR_FORMAT_VERSION = "tau-json-validation/1"
EXTRACTION_MAX_REPAIRS = 2


class GenerationError(ValueError):
    def __init__(
        self,
        reason: str,
        responses: list[str],
        configuration_error: dict[str, Any] | None = None,
        *,
        attempts: int | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
        last_error: str | None = None,
    ):
        super().__init__(reason)
        self.responses = responses
        self.configuration_error = configuration_error
        self.attempts = len(responses) if attempts is None else attempts
        self.validation_errors = validation_errors or []
        self.last_error = last_error


def _path_from_message(message: str) -> str:
    match = re.match(r"^(?P<path>/[^:]*):\s*(?P<message>.*)$", message)
    return match.group("path") if match else "/"


def _schema_branch(error: ValidationError) -> str:
    """Summarize the useful part of a jsonschema branch without dumping it."""

    def describe(item: ValidationError) -> str:
        if item.validator == "const":
            return f"kind={item.validator_value!r}"
        if item.validator == "enum":
            return f"one of {item.validator_value!r}"
        if item.validator == "required":
            return f"object containing {item.validator_value!r}"
        if item.validator == "type":
            return f"type {item.validator_value!r}"
        if item.validator == "properties":
            return "object with the declared properties"
        if item.validator in {"oneOf", "anyOf"}:
            children = [describe(child) for child in item.context]
            return "one of: " + "; ".join(children[:6])
        return item.validator or "the declared schema"

    return describe(error)[:1000]


def _validation_error(error: Exception, attempt: int) -> dict[str, Any]:
    if isinstance(error, ValidationError):
        path = "/" + "/".join(escape(str(part)) for part in error.absolute_path)
        message = error.message
        expected = _schema_branch(error)
        instance = error.instance
        if isinstance(instance, dict) and instance.get("kind") == "absolute_time":
            expected = (
                "absolute_time must be used as a temporal_anchor, not as a direct "
                "parameter value"
            )
    else:
        raw = str(error)
        path = _path_from_message(raw)
        message = raw.split(":", 1)[1].strip() if path != "/" else raw
        expected = "valid JSON matching the declared schema and semantic checks"
    return {
        "path": path,
        "message": message,
        "expected_structure": expected,
        "attempt": attempt,
    }


def _repair_prompt(base: str, raw: str, error: dict[str, Any]) -> str:
    return (
        base
        + "\nFORMAT REPAIR ONLY. Preserve the extracted meanings and every raw value. "
        "Do not use a reference answer, hidden state, or database state. Correct "
        "only the structure required by the schema and coverage contract. The "
        "previous output and its validation error are:\n"
        + canonical({"output": raw, "validation_error": error})
    )


async def generate_json(
    model: JsonModel,
    prompt: str,
    schema: dict[str, Any],
    check: Callable[[dict[str, Any]], None] | None = None,
    *,
    max_repairs: int = 1,
) -> tuple[dict[str, Any], list[str]]:
    if (
        not isinstance(max_repairs, int)
        or isinstance(max_repairs, bool)
        or max_repairs < 0
    ):
        raise ValueError("max_repairs must be a nonnegative integer")
    responses: list[str] = []
    validation_errors: list[dict[str, Any]] = []
    validator = validator_for(schema)(schema)
    generation_context = "\nGeneration contract: " + canonical(
        {
            "generation_version": GENERATION_VERSION,
            "max_repairs": max_repairs,
            "validation_error_format_version": VALIDATION_ERROR_FORMAT_VERSION,
        }
    )
    request_base = prompt + generation_context
    request_prompt = request_base
    for attempt in range(max_repairs + 1):
        try:
            raw = await model.complete(request_prompt, schema)
        except ModelConfigurationError as exc:
            raise GenerationError(
                type(exc).__name__,
                responses,
                exc.diagnostic,
                attempts=len(responses),
                validation_errors=validation_errors,
            ) from None
        except Exception as exc:
            raise GenerationError(
                type(exc).__name__,
                responses,
                attempts=len(responses),
                validation_errors=validation_errors,
                last_error=str(exc),
            ) from None
        responses.append(raw)
        try:
            value = json.loads(raw)
            validator.validate(value)
            if check:
                check(value)
            return value, responses
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            failure = exc
        except ValidationError as exc:
            failure = exc
        else:
            raise
        diagnostic = _validation_error(failure, attempt + 1)
        validation_errors.append(diagnostic)
        if attempt < max_repairs:
            request_prompt = _repair_prompt(request_base, raw, diagnostic)
    label = "repair" if max_repairs == 1 else "repairs"
    raise GenerationError(
        f"Invalid model structure after {max_repairs} {label}",
        responses,
        attempts=len(responses),
        validation_errors=validation_errors,
        last_error=validation_errors[-1]["message"] if validation_errors else None,
    )


def provenance(
    prompt: str,
    schema: dict[str, Any],
    model: ModelConfig,
    *,
    max_repairs: int = 1,
) -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "generation_version": GENERATION_VERSION,
        "max_repairs": max_repairs,
        "validation_error_format_version": VALIDATION_ERROR_FORMAT_VERSION,
        "prompt_sha256": digest(prompt),
        "schema_sha256": digest(schema),
        "model": model.model_dump(),
    }

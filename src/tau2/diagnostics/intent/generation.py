"""Common mechanics only: schema validation, one format repair, and provenance."""

import json
from typing import Any, Callable

from jsonschema.validators import validator_for

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.config import ModelConfig, ModelConfigurationError
from tau2.diagnostics.intent.io import canonical, digest, escape
from tau2.diagnostics.intent.types import JsonModel


class GenerationError(ValueError):
    def __init__(
        self,
        reason: str,
        responses: list[str],
        configuration_error: dict[str, Any] | None = None,
    ):
        super().__init__(reason)
        self.responses = responses
        self.configuration_error = configuration_error


async def generate_json(
    model: JsonModel,
    prompt: str,
    schema: dict[str, Any],
    check: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    responses: list[str] = []
    validator = validator_for(schema)(schema)
    for attempt in range(2):
        try:
            raw = await model.complete(prompt, schema)
        except ModelConfigurationError as exc:
            raise GenerationError(
                type(exc).__name__, responses, exc.diagnostic
            ) from None
        except Exception as exc:
            raise GenerationError(type(exc).__name__, responses) from None
        responses.append(raw)
        try:
            value = json.loads(raw)
            validator.validate(value)
            if check:
                check(value)
            return value, responses
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            error = str(exc)
        except Exception as exc:
            from jsonschema.exceptions import ValidationError

            if not isinstance(exc, ValidationError):
                raise
            path = "/".join(escape(str(part)) for part in exc.absolute_path)
            error = f"/{path}: {exc.message}"
        if attempt == 0:
            prompt += (
                "\nFORMAT REPAIR ONLY. Preserve the extracted meanings and raw values. "
                "Do not use the reference to improve the prediction. Correct this "
                "invalid output to satisfy the schema and coverage contract:\n"
                + canonical({"output": raw, "validation_error": error})
            )
    raise GenerationError("Invalid model structure after one repair", responses)


def provenance(
    prompt: str, schema: dict[str, Any], model: ModelConfig
) -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "prompt_sha256": digest(prompt),
        "schema_sha256": digest(schema),
        "model": model.model_dump(),
    }

"""Stage 2: independent backend-input extraction. This interface cannot accept a GT."""

from typing import Any

from tau2.diagnostics.intent.config import (
    ModelConfig,
    ScoringProfile,
    ValidationProfile,
)
from tau2.diagnostics.intent.extraction import extract
from tau2.diagnostics.intent.types import JsonModel, TextRecord


async def extract_prediction(
    record: TextRecord,
    schema: dict[str, Any],
    instructions: str,
    profile: ScoringProfile,
    model: JsonModel,
    model_config: ModelConfig,
    validation_profile: ValidationProfile | None = None,
    definition_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await extract(
        record,
        schema,
        instructions,
        profile,
        model,
        model_config,
        "prediction",
        validation_profile,
        definition_metadata,
    )

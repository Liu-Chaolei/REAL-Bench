"""Question packaging preserves legacy data and keeps answers private."""

import pytest
from pydantic import ValidationError

from tau2.data_model.scenario import ScenarioSpec


def original_spec():
    """Minimal historical payload with all original serialized fields."""
    return {
        "version": "scenario-v1",
        "start_time": 0.0,
        "timezone": "UTC",
        "source_id": "original",
        "source_sha256": "a" * 64,
        "snapshot": "b" * 64,
        "category": "execution",
        "events": [],
        "runtime_sha256": None,
        "assets_sha256": None,
    }


def test_single_stage_payload_keeps_its_original_serialized_shape():
    payload = original_spec()
    assert ScenarioSpec.model_validate(payload).model_dump(mode="json") == payload


def test_inline_stage_answers_survive_native_model_roundtrip():
    payload = original_spec()
    payload["stages"] = [
        {
            "stage": 1,
            "request": "Send the actual invoice by email.",
            "event_ids": ["series_s1_invoice"],
            "criteria": {"reward_basis": ["ENV_ASSERTION"]},
            "evidence": [{"public_tool": "Emails__get_email_by_id"}],
            "checks": [{"operation": "equals", "expected": "Invoice"}],
        }
    ]
    parsed = ScenarioSpec.model_validate(payload)
    restored = ScenarioSpec.model_validate_json(parsed.model_dump_json())
    assert restored.stages[0].evidence == payload["stages"][0]["evidence"]
    assert restored.stages[0].checks == payload["stages"][0]["checks"]
    assert restored.stages[0].request == payload["stages"][0]["request"]


def test_generation_acceptance_logs_are_not_native_stage_fields():
    payload = original_spec()
    payload["stages"] = [
        {
            "stage": 1,
            "request": "Send the actual invoice by email.",
            "event_ids": [],
            "criteria": {},
            "verification": {"valid": True},
        }
    ]
    with pytest.raises(ValidationError, match="verification"):
        ScenarioSpec.model_validate(payload)

"""Select the initial request using public, actually delivered input evidence."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def timestamp(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def initial_request_boundary(
    events: list[dict[str, Any]], path: Path | None
) -> dict[str, Any]:
    if path is None or not path.is_file():
        raise ValueError("missing_input_scope_evidence")
    raw = path.read_bytes()
    observation = json.loads(raw)
    if (
        not isinstance(observation, dict)
        or observation.get("input_complete") is not True
    ):
        raise ValueError("incomplete_original_input")
    inputs = observation.get("user_inputs")
    if inputs is None:
        # Older non-simulator observations have only the original input.
        if observation.get("user_simulator") is not None or any(
            str(e.get("type", "")).startswith("user_simulator.input.") for e in events
        ):
            raise ValueError("missing_input_scope_evidence")
        inputs = []
    if not isinstance(inputs, list):
        raise ValueError("invalid_input_scope_evidence")
    followups = []
    identities = set()
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("identity"), str):
            raise ValueError("invalid_input_scope_evidence")
        identity = item["identity"]
        if identity in identities:
            raise ValueError("invalid_input_scope_evidence")
        identities.add(identity)
        if identity == "original":
            if item.get("complete") is not True:
                raise ValueError("incomplete_original_input")
            continue
        frames = item.get("timeline")
        if not isinstance(frames, list):
            raise ValueError("invalid_input_scope_evidence")
        for frame in frames:
            if (
                not isinstance(frame, dict)
                or not timestamp(frame.get("start_mono"))
                or not timestamp(frame.get("end_mono"))
                or frame["end_mono"] <= frame["start_mono"]
                or not isinstance(frame.get("sample_count"), int)
                or frame["sample_count"] <= 0
            ):
                raise ValueError("invalid_input_scope_evidence")
        if frames:
            followups.append((min(f["start_mono"] for f in frames), identity))
        elif item.get("complete") is True:
            raise ValueError("invalid_input_scope_evidence")
    if (
        inputs or observation.get("user_simulator") is not None
    ) and "original" not in identities:
        raise ValueError("missing_input_scope_evidence")
    # Every completed/ongoing send must have its observation row. Discarded
    # candidates have no delivered frames and deliberately do not set a boundary.
    discarded = {
        e.get("identity")
        for e in events
        if e.get("type") == "user_simulator.input.discarded"
    }
    for event in events:
        if event.get("type") in {
            "user_simulator.input.started",
            "user_simulator.input.completed",
        }:
            if event.get("identity") not in identities | discarded:
                raise ValueError("missing_input_scope_evidence")
    boundary, identity = min(followups) if followups else (None, None)
    return {
        "observation_sha256": hashlib.sha256(raw).hexdigest(),
        "scope_boundary_mono": boundary,
        "scope_boundary_input": identity,
        "scope_boundary_source": "delivered_followup_audio"
        if followups
        else "no_delivered_followup",
    }


def scope_context(scope: str) -> dict[str, str]:
    # This is the only extra information entering model prompts. Observation
    # contents, simulator reasoning and task metadata remain provenance only.
    return (
        {"backend_input_scope": "initial_request"} if scope == "initial_request" else {}
    )


INITIAL_REQUEST_POLICY = """When input context declares backend_input_scope=initial_request,
the evaluator has selected all backend submissions attributable to the initial
user request, before subsequent user clarification. All selected messages jointly
define the request being evaluated; do not treat earlier selected messages as
irrelevant history merely because another selected message comes last. Keep their
boundaries and resolve explicit corrections within this selected scope. Preserve
business requirements in attached context when they belong to that request.
Embedded routing instructions about the 'current' message do not change this
evaluation scope. Do not infer excluded later user turns or repair missing intent.
"""

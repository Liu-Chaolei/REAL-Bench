"""Stage 3: independently check each intent against the other side's raw text."""

import asyncio
from typing import Any

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.backend_scope import INITIAL_REQUEST_POLICY
from tau2.diagnostics.intent.config import (
    ModelConfig,
    ScoringProfile,
    ValidationProfile,
)
from tau2.diagnostics.intent.generation import generate_json, provenance
from tau2.diagnostics.intent.io import canonical, digest
from tau2.diagnostics.intent.types import JsonModel
from tau2.diagnostics.intent.units import build_items

METHOD = "bidirectional_text"
DIRECTIONS = ("forward", "backward")

EVALUATION_POLICY = """Check the supplied immutable intent items against target_input.text
and explicitly allowed target_input.context. The complete source intent supplies
object references, goal ownership, branches, condition scope and temporal anchors.
Do not reconstruct or improve either input. Do not invent, split, merge or drop items.
The other side's extracted intent is deliberately not provided. You are checking
meaning in raw text, not matching JSON nodes or paths. Multiple passages may jointly
support an item, and one passage may support several items. Ignore arbitrary ID
spelling, but retain the actual identity, roles and relationships those IDs express.
Check the complete requested operation, attribute, quantity, unit, strict/inclusive
boundary, negation, object set, logical relation, branch and temporal scope. Mere
logical implication is insufficient: changing a selection threshold changes the task.
A request requiring at least two emails is not equivalent to one requiring at least
one. Interpret compound names by their full meaning, not just matching words.
Use matched for the same business requirement, even when current-value baselines,
units or result transformations are expressed implicitly by an unambiguous operation.
A wrong value, role, condition or scope is changed. Use uncertain when the meaning
cannot be determined reliably. Respect the backend message's declared precedence
between final_asr, objective and context; do not cherry-pick a matching passage while
ignoring an unresolved contradiction, another request, a negation or its authority.
Only the current business request counts. Protocol/routing fields and generic
coordination or response-format instructions are not additional business requirements.
An implementation explanation does not automatically add an independent user goal;
retain explicitly required methods, restrictions or extra outcomes. Authentication
scope is not automatically an object-ownership filter.
Use ignored ONLY for an item which merely organizes the representation (such as a
result placeholder or bookkeeping output binding), without independent business
meaning. Never ignore real object types, requested operations, parameter values,
conditions, quantities, relations or times. Eligible item types are marked by the
program; eligibility alone is not a reason to ignore an item. If unsure, use uncertain.
Return one judgment for EVERY supplied item_id exactly once, with status and reason.
Optional evidence contains verbatim passages from target_input, not source values
copied as supposed evidence. Return only JSON conforming to the supplied schema."""

EVALUATION_POLICY += """For whole_call inputs, 'current business request' includes
the complete chronological authorization history in the declared context policy.
An old parameter correctly submitted while authorized is not an extra or changed
fact merely because a later request amended it. Missing/wrong updates and wrong
cancellation targets remain errors. Transmission content and native acceptance
are distinct facts; an accepted update is not evidence of business completion.
Use the expressed target relationships and times, with no invented time limit.
"""


def judgment_schema(direction: str) -> dict[str, Any]:
    if direction not in DIRECTIONS:
        raise ValueError("Unknown evaluation direction")
    absent = "missing" if direction == "forward" else "extra"
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "item_id": {"type": "string"},
                        "status": {
                            "enum": [
                                "matched",
                                absent,
                                "changed",
                                "uncertain",
                                "ignored",
                            ]
                        },
                        "reason": {"type": "string", "minLength": 1},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["item_id", "status", "reason"],
                },
            }
        },
        "required": ["judgments"],
    }


def can_ignore(item: dict[str, Any]) -> bool:
    """Limit bookkeeping classification; business facts cannot silently disappear."""
    return item.get("unit_type") == "output_binding" or all(
        path.endswith("/entity_type") for path in item["source_paths"]
    )


def validate_judgments(
    value: dict[str, Any], items: list[dict[str, Any]], direction: str
) -> None:
    from jsonschema.validators import validator_for

    schema = judgment_schema(direction)
    validator_for(schema)(schema).validate(value)
    inventory = {item["item_id"]: item for item in items}
    if len(inventory) != len(items):
        raise ValueError("Duplicate source item IDs")
    seen = set()
    for row in value["judgments"]:
        identity = row["item_id"]
        if identity not in inventory or identity in seen:
            raise ValueError("Unknown or duplicated item ID")
        if row["status"] == "ignored" and not can_ignore(inventory[identity]):
            raise ValueError("A business fact cannot be ignored")
        seen.add(identity)
    if seen != set(inventory):
        raise ValueError("Judgments must cover every source item")


def materialize(
    value: dict[str, Any], items: list[dict[str, Any]], direction: str
) -> list[dict[str, Any]]:
    validate_judgments(value, items, direction)
    decisions = {row["item_id"]: row for row in value["judgments"]}
    return [item | decisions[item["item_id"]] for item in items]


def raw_input(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    value = (artifact or {}).get("input")
    if not isinstance(value, dict) or value.get("unavailable_reason"):
        return None
    text = value.get("text")
    metadata = value.get("metadata") or {}
    not_sent = (
        isinstance(metadata, dict)
        and metadata.get("source_kind") == "backend_input"
        and metadata.get("backend_input_status") == "not_sent"
        and text == ""
    )
    if not isinstance(text, str) or (not text.strip() and not not_sent):
        return None
    # Task metadata, provenance and hidden annotations never enter judge prompts.
    return {"text": text, "context": value.get("context") or {}}


async def check_direction(
    direction: str,
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
    profile: ScoringProfile,
    model: JsonModel,
    model_config: ModelConfig,
    schema: dict[str, Any] | None,
    validation: ValidationProfile | None,
    definition: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "direction": direction,
        "items": [],
        "rows": [],
        "raw_responses": [],
    }
    if source is None or source.get("status") != "complete":
        return result | {
            "status": "unavailable",
            "reason": "incomplete_source_extraction",
        }
    target_input = raw_input(target)
    if target_input is None:
        return result | {"status": "unavailable", "reason": "missing_target_text"}
    try:
        from tau2.diagnostics.intent.gaia2_units import effective_validation
        from tau2.diagnostics.intent.units import validate_references
        from tau2.diagnostics.intent.validation import validate_intent

        contract = effective_validation(profile, validation)
        if schema is not None:
            from jsonschema.validators import validator_for

            if source.get("schema_sha256") != digest(schema):
                return result | {
                    "status": "unavailable",
                    "reason": "configured_schema_mismatch",
                }
            validator_for(schema)(schema).validate(source["intent"])
        validate_references(source["intent"], profile)
        validate_intent(source["intent"], contract)
        items = build_items(source["intent"], profile, contract)
        items = [item | {"ignore_eligible": can_ignore(item)} for item in items]
        policy = (
            "Forward: check GT requirements in the backend's actual message. "
            "An absent requirement is missing."
            if direction == "forward"
            else "Backward: check prediction requirements in the original user input. "
            "An unsupported new requirement is extra; an altered counterpart is changed."
        )
        payload = {
            "source_intent": source["intent"],
            "items": items,
            "target_input": target_input,
            "intent_schema": schema,
        }
        prompt = (
            EVALUATION_POLICY
            + "\n"
            + INITIAL_REQUEST_POLICY
            + "\n"
            + policy
            + "\nEvidence:\n"
            + canonical(payload)
        )
        response_schema = judgment_schema(direction)
        fingerprint = digest(
            {
                "pipeline_version": PIPELINE_VERSION,
                "method": METHOD,
                "direction": direction,
                "prompt": prompt,
                "response_schema": response_schema,
                "profile": profile.model_dump(),
                "validation": contract.model_dump(),
                "definition": definition,
                "model": model_config.model_dump(),
            }
        )
        result |= {
            "items": items,
            "items_sha256": digest(items),
            "source_intent_sha256": digest(source["intent"]),
            "target_input_sha256": digest(target_input),
            "fingerprint": fingerprint,
            "provenance": provenance(prompt, response_schema, model_config),
        }
        if (
            previous
            and previous.get("fingerprint") == fingerprint
            and previous.get("status") == "complete"
        ):
            rows = materialize(previous["judgments"], items, direction)
            if previous.get("rows") == rows and previous.get("items_sha256") == digest(
                items
            ):
                return previous
        if not items:
            value, raw = {"judgments": []}, []
        elif direction == "forward" and target_input["text"] == "":
            value = {
                "judgments": [
                    {
                        "item_id": item["item_id"],
                        "status": "missing",
                        "reason": "Confirmed no backend task submission",
                    }
                    for item in items
                ]
            }
            raw = []
        else:
            value, raw = await generate_json(
                model,
                prompt,
                response_schema,
                lambda value: validate_judgments(value, items, direction),
            )
        rows = materialize(value, items, direction)
        return result | {
            "status": "uncertain"
            if any(row["status"] == "uncertain" for row in rows)
            else "complete",
            "judgments": value,
            "rows": rows,
            "raw_responses": raw,
        }
    except Exception as exc:
        return result | {
            "status": "error",
            "reason": type(exc).__name__,
            "configuration_error": getattr(exc, "configuration_error", None),
            "raw_responses": getattr(exc, "responses", []),
            "attempts": getattr(exc, "attempts", 0),
            "validation_errors": getattr(exc, "validation_errors", []),
            "last_error": getattr(exc, "last_error", None),
        }


async def evaluate(
    gt: dict[str, Any] | None,
    prediction: dict[str, Any] | None,
    profile: ScoringProfile,
    model: JsonModel,
    model_config: ModelConfig,
    schema: dict[str, Any] | None = None,
    validation_profile: ValidationProfile | None = None,
    definition_metadata: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if gt and prediction and gt["id"] != prediction["id"]:
        raise ValueError("Cannot evaluate different request IDs")
    result: dict[str, Any] = {
        "method": METHOD,
        "id": (gt or prediction or {}).get("id"),
        "stage": "evaluation",
        "profile_sha256": digest(profile.model_dump()),
        "metadata": (gt or prediction or {}).get("input", {}).get("metadata", {}),
        "definition": definition_metadata or {"name": "custom"},
        "gt_sha256": digest(gt.get("intent")) if gt else None,
        "prediction_sha256": digest(prediction.get("intent")) if prediction else None,
    }
    async def run_direction(direction, source, target):
        return direction, await check_direction(
            direction,
            source,
            target,
            profile,
            model,
            model_config,
            schema,
            validation_profile,
            definition_metadata,
            (previous or {}).get(direction),
        )

    directions = await asyncio.gather(
        run_direction("forward", gt, prediction),
        run_direction("backward", prediction, gt),
    )
    result.update(dict(directions))
    statuses = {result[name]["status"] for name in DIRECTIONS}
    result["status"] = next(
        state
        for state in ("error", "unavailable", "uncertain", "complete")
        if state in statuses
    )
    result["raw_responses"] = [
        raw for name in DIRECTIONS for raw in result[name]["raw_responses"]
    ]
    if result["status"] != "complete":
        result["reason"] = "; ".join(
            name + ":" + result[name].get("reason", result[name]["status"])
            for name in DIRECTIONS
            if result[name]["status"] != "complete"
        )
    return result

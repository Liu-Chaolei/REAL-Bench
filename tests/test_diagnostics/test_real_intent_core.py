import json
from copy import deepcopy

import pytest

from tau2.diagnostics.intent.config import IntentConfig, ModelConfig
from tau2.diagnostics.intent.evaluation import evaluate
from tau2.diagnostics.intent.generation import GenerationError, generate_json
from tau2.diagnostics.intent.io import digest
from tau2.diagnostics.intent.scoring import score_batch, score_record
from tau2.diagnostics.intent.units import build_items

CONFIG = IntentConfig(definition_set="gaia2")
(SCHEMA, PROFILE, VALIDATION) = (
    CONFIG.load_schema(),
    CONFIG.profile(),
    CONFIG.validation(),
)
MODEL = ModelConfig(model="fixture/model")


@pytest.mark.asyncio
async def test_generation_uses_two_bounded_repairs_and_structured_errors():
    schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "object",
                "properties": {"kind": {"const": "literal"}},
                "required": ["kind"],
                "additionalProperties": False,
            }
        },
        "required": ["intent"],
        "additionalProperties": False,
    }

    class Model:
        def __init__(self):
            self.prompts = []
            self.responses = [
                '{"intent":{"kind":"wrong"}}',
                '{"intent":{"kind":null}}',
                '{"intent":{"kind":"literal"}}',
            ]

        async def complete(self, prompt, schema):
            self.prompts.append(prompt)
            return self.responses.pop(0)

    model = Model()
    value, responses = await generate_json(model, "extract", schema, max_repairs=2)
    assert value == {"intent": {"kind": "literal"}}
    assert len(responses) == len(model.prompts) == 3
    assert "/intent/kind" in model.prompts[1]
    assert "expected_structure" in model.prompts[1]
    assert "/intent/kind" in model.prompts[2]
    assert "generation_version" in model.prompts[0]


@pytest.mark.asyncio
async def test_generation_error_preserves_attempts_without_turning_failure_into_score():
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    class Model:
        async def complete(self, prompt, schema):
            return '{"value":1}'

    with pytest.raises(GenerationError) as captured:
        await generate_json(Model(), "extract", schema, max_repairs=2)
    error = captured.value
    assert error.attempts == 3
    assert len(error.responses) == len(error.validation_errors) == 3
    assert error.validation_errors[-1]["path"] == "/value"
    assert error.last_error


@pytest.mark.asyncio
async def test_generation_provider_error_is_not_retried():
    calls = 0

    class Model:
        async def complete(self, prompt, schema):
            nonlocal calls
            calls += 1
            raise TimeoutError("provider timeout")

    with pytest.raises(GenerationError) as captured:
        await generate_json(Model(), "extract", {"type": "object"}, max_repairs=2)
    assert calls == 1
    assert captured.value.attempts == 0
    assert captured.value.last_error == "provider timeout"


class Fake:
    def __init__(self, responses):
        self.responses = responses
        self.prompts = []
        self.schemas = []

    async def complete(self, prompt, schema):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return json.dumps(
            self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        )


def record(doc, text="Original current request", **overrides):
    return {
        "id": "request",
        "status": "complete",
        "intent": doc,
        "schema_sha256": digest(SCHEMA),
        "input": {"text": text, "context": {}, "metadata": {"secret": "HIDDEN_META"}},
    } | overrides


def decisions(doc, statuses=None):
    statuses = statuses or {}
    return {
        "judgments": [
            {
                "item_id": item["item_id"],
                "status": statuses.get(item["source_paths"][0], "matched"),
                "reason": "Authored semantic fixture",
            }
            for item in build_items(doc, PROFILE, VALIDATION)
        ]
    }


def notify(recipient=None):
    goal = {"id": "notification", "action": "notify_user"}
    if recipient:
        goal["parameters"] = [
            {"name": "recipient", "value": {"kind": "literal", "value": recipient}}
        ]
    return {"goals": [goal]}


@pytest.mark.asyncio
async def test_directional_counts_with_different_inventories():
    (gdoc, pdoc) = (notify("Alice"), notify("Bob"))
    pdoc["goals"][0]["parameters"].append(
        {"name": "priority", "value": {"kind": "literal", "value": "high"}}
    )
    model = Fake(
        [
            decisions(gdoc, {"/goals/0/parameters/0": "changed"}),
            decisions(
                pdoc,
                {"/goals/0/parameters/0": "changed", "/goals/0/parameters/1": "extra"},
            ),
        ]
    )
    result = await evaluate(
        record(gdoc), record(pdoc), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    score = score_record(result)
    assert score["recall"] == 1 / 2 and score["precision"] == 1 / 3
    assert score["f1"] == pytest.approx(0.4)
    assert "tp" not in score and "fp" not in score and ("fn" not in score)
    assert score["forward"]["counts"]["changed"] == 1
    assert score["backward"]["counts"]["extra"] == 1
    tampered = deepcopy(result)
    tampered["forward"]["rows"][0]["status"] = "missing"
    with pytest.raises(ValueError, match="Rows differ"):
        score_record(tampered)
    tampered = deepcopy(result)
    tampered["backward"]["items"][0]["values"] = ["altered"]
    with pytest.raises(ValueError, match="checksum"):
        score_record(tampered)


@pytest.mark.asyncio
async def test_one_direction_transport_failure_does_not_erase_other():
    class TransportFailure(Fake):
        async def complete(self, prompt, schema):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                raise ConnectionError("offline")
            return json.dumps(decisions(notify()))

    model = TransportFailure([])
    result = await evaluate(
        record(notify()), record(notify()), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    score = score_record(result)
    assert result["forward"]["status"] == "error"
    assert score["precision"] == 1 and score["recall"] is None and (score["f1"] is None)
    assert len(model.prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_side", ["gt", "prediction"])
async def test_extraction_failure_still_allows_other_direction(failed_side):
    (g, p) = (record(notify()), record(notify()))
    (g if failed_side == "gt" else p).update(status="error", intent=None)
    model = Fake([decisions(notify())])
    result = await evaluate(g, p, PROFILE, model, MODEL, SCHEMA, VALIDATION)
    score = score_record(result)
    assert (score["precision"] if failed_side == "gt" else score["recall"]) == 1
    assert score["f1"] is None and len(model.prompts) == 1


@pytest.mark.asyncio
async def test_offline_record_status_cannot_hide_uncertainty():
    doc = notify()
    result = await evaluate(
        record(doc),
        record(doc),
        PROFILE,
        Fake([decisions(doc), decisions(doc, {"/goals/0/action": "uncertain"})]),
        MODEL,
        SCHEMA,
        VALIDATION,
    )
    result["status"] = "complete"
    with pytest.raises(ValueError, match="direction statuses"):
        score_record(result)


@pytest.mark.asyncio
async def test_missing_original_text_is_not_an_empty_intent():
    g = record(notify())
    del g["input"]
    model = Fake([decisions(notify())])
    result = await evaluate(
        g, record(notify()), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    assert score_record(result)["recall"] == 1
    assert result["backward"]["reason"] == "missing_target_text"
    assert score_record(result)["precision"] is None


@pytest.mark.asyncio
async def test_empty_inventory_does_not_imply_empty_other_text():
    empty = record({"goals": []}, "Front says nothing actionable")
    model = Fake([decisions(notify(), {"/goals/0/action": "extra"})])
    result = await evaluate(
        empty, record(notify()), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    assert score_record(result)["precision"] == 0
    assert score_record(result)["recall"] == 1
    assert score_record(result)["f1"] == 0 and len(model.prompts) == 1


@pytest.mark.asyncio
async def test_ignored_bookkeeping_not_counted_and_business_cannot_be_ignored():
    doc = {
        "entities": [{"id": "r", "entity_type": "computed_value"}],
        "goals": [
            {
                "id": "g",
                "action": "compute_average",
                "outputs": [{"entity_ref": "r", "role": "computed_result"}],
            }
        ],
    }
    value = decisions(
        doc, {"/entities/0/entity_type": "ignored", "/goals/0/outputs/0": "ignored"}
    )
    model = Fake([value, value])
    result = await evaluate(
        record(doc), record(doc), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    score = score_record(result)
    assert score["f1"] == 1 and score["forward"]["counts"]["total"] == 1
    assert score["forward"]["counts"]["ignored"] == 2
    from tau2.diagnostics.intent.evaluation import validate_judgments

    with pytest.raises(ValueError, match="cannot be ignored"):
        validate_judgments(
            decisions(notify(), {"/goals/0/action": "ignored"}),
            build_items(notify(), PROFILE, VALIDATION),
            "forward",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "status"])
async def test_single_repair_covers_invalid_direction_responses(mutation):
    good = decisions(notify())
    bad = deepcopy(good)
    if mutation == "duplicate":
        bad["judgments"] *= 2
    elif mutation == "missing":
        bad["judgments"] = []
    elif mutation == "unknown":
        bad["judgments"][0]["item_id"] = "invented"
    else:
        bad["judgments"][0]["status"] = "extra"
    model = Fake([bad, good, good])
    result = await evaluate(
        record(notify()), record(notify()), PROFILE, model, MODEL, SCHEMA, VALIDATION
    )
    assert score_record(result)["f1"] == 1 and len(model.prompts) == 3
    assert "FORMAT REPAIR ONLY" in model.prompts[1]
    assert len(result["forward"]["raw_responses"]) == 2


@pytest.mark.asyncio
async def test_micro_uses_same_complete_cohort_and_directional_denominators():
    good = await evaluate(
        record(notify()),
        record(notify()),
        PROFILE,
        Fake([decisions(notify()), decisions(notify())]),
        MODEL,
        SCHEMA,
        VALIDATION,
    )
    uncertain = await evaluate(
        record(notify()),
        record(notify()),
        PROFILE,
        Fake(
            [decisions(notify()), decisions(notify(), {"/goals/0/action": "uncertain"})]
        ),
        MODEL,
        SCHEMA,
        VALIDATION,
    )
    uncertain["id"] = "other"
    good["metadata"] = {"config": "search"}
    uncertain["metadata"] = {"config": "execution"}
    score = score_batch([good, uncertain], ["config"])
    assert score["macro_f1"] == 1
    assert score["evaluation_log"]["coverage"]["rate"] == 0.5
    assert score["precision"] == score["recall"] == 1
    assert score["groups"]["config"]["execution"]["macro_f1"] is None
    assert "micro" not in score and "complete_match_rate" not in score
    with pytest.raises(ValueError, match="method=bidirectional_text"):
        score_record({"id": "unsupported", "status": "complete"})


@pytest.mark.asyncio
async def test_inline_reference_and_split_rounding_need_no_global_mapping():
    selection = {
        "entity_type": "property",
        "condition": {
            "kind": "all",
            "items": [
                {
                    "kind": "comparison",
                    "field": field,
                    "operator": "==",
                    "value": {"kind": "literal", "value": 2},
                }
                for field in ("bedrooms", "bathrooms")
            ],
        },
    }
    rounding = {"name": "round_to_nearest", "value": {"kind": "literal", "value": 1000}}
    gdoc = {
        "goals": [
            {
                "id": "one",
                "action": "calculate_average_price",
                "target": {"kind": "selection", "selection": selection},
                "parameters": [rounding],
            }
        ]
    }
    pdoc = {
        "entities": [
            {"id": "properties", "entity_type": "property", "selection": selection},
            {"id": "result", "entity_type": "computed_value"},
        ],
        "goals": [
            {
                "id": "compute",
                "action": "calculate_average_price",
                "target": {"kind": "entity_ref", "ref": "properties"},
                "outputs": [{"entity_ref": "result", "role": "calculated_value"}],
            },
            {
                "id": "format",
                "action": "round",
                "target": {"kind": "entity_ref", "ref": "result"},
                "parameters": [rounding],
            },
        ],
    }
    model = Fake(
        [
            decisions(gdoc),
            decisions(
                pdoc,
                {"/entities/1/entity_type": "ignored", "/goals/0/outputs/0": "ignored"},
            ),
        ]
    )
    text = "Average the prices of properties with two bedrooms and two bathrooms, then round that average to the nearest thousand."
    result = await evaluate(
        record(gdoc, text),
        record(pdoc, text),
        PROFILE,
        model,
        MODEL,
        SCHEMA,
        VALIDATION,
    )
    assert score_record(result)["f1"] == 1
    assert len(result["forward"]["items"]) != len(result["backward"]["items"])
    assert result["forward"]["items"][1]["source_paths"] == ["/goals/0/parameters/0"]
    assert '"ref":"result"' in model.prompts[1]
    assert '"value":1000' in model.prompts[1]
    assert len(model.prompts) == 2

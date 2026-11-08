"""Duplicate delegation evidence, deterministic penalties and failed-check coverage."""

import json
from copy import deepcopy

import pytest

from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.call import combined_sources, evaluate_call
from tau2.diagnostics.intent.config import IntentConfig, ModelConfig
from tau2.diagnostics.intent.evaluation import evaluate
from tau2.diagnostics.intent.extraction import extract
from tau2.diagnostics.intent.io import canonical, digest, write_json
from tau2.diagnostics.intent.repetition import (
    apply_repetition,
    check_repetition,
    goal_paths,
    validate_goal_evidence,
    validate_judgment,
)
from tau2.diagnostics.intent.scoring import score_record
from tau2.diagnostics.intent.types import TextRecord
from tau2.diagnostics.intent.units import build_items
from tau2.diagnostics.model import RequestBudget

DEFINITION = IntentConfig(definition_set="gaia2")
SCHEMA, PROFILE, VALIDATION = (
    DEFINITION.load_schema(),
    DEFINITION.profile(),
    DEFINITION.validation(),
)
MODEL = ModelConfig(model="fixture/model")
TEXT = "Send Wang an email saying the meeting is at 15:00."


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    async def complete(self, prompt, schema):
        self.prompts.append(prompt)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


def document(count=3):
    return {
        "goals": [
            {
                "id": f"g{i}",
                "action": "send_email",
                "parameters": [
                    {
                        "name": "recipient",
                        "value": {"kind": "literal", "value": "Wang"},
                    },
                    {
                        "name": "content",
                        "value": {"kind": "literal", "value": "Meeting at 15:00"},
                    },
                ],
            }
            for i in range(count)
        ]
    }


def artifacts(count=3, user_text=TEXT):
    user = [{"source_id": "u", "role": "user", "text": user_text}]
    backend = [
        {"source_id": f"b{i}", "role": "prompt", "content": TEXT, "session_id": f"s{i}"}
        for i in range(count)
    ]
    gt = {
        "id": "call",
        "status": "complete",
        "intent": document(1),
        "schema_sha256": digest(SCHEMA),
        "input": {"text": canonical(user), "context": {}},
    }
    prediction = {
        "id": "call",
        "status": "complete",
        "intent": document(count),
        "schema_sha256": digest(SCHEMA),
        "input": {"text": canonical(backend), "context": {}},
        "evidence": [
            {"path": f"/goals/{i}", "source_id": f"b{i}", "quote": TEXT}
            for i in range(count)
        ],
    }
    return gt, prediction


def judgment(count=3, allowed=1, user_text=TEXT):
    paths = goal_paths(document(count))
    return {
        "checked_goal_paths": paths,
        "groups": [
            {
                "goal_paths": paths,
                "allowed_count": allowed,
                "extra_goal_paths": paths[min(allowed, count) :]
                if allowed is not None
                else [],
                "reason": "Same independently delegated operation with source evidence",
                "evidence": [{"side": "user", "source_id": "u", "quote": user_text}]
                + [
                    {"side": "backend", "source_id": f"b{i}", "quote": TEXT}
                    for i in range(count)
                ],
            }
        ],
        "uncertain_goal_paths": paths if allowed is None else [],
    }


def decisions(doc, overrides=None):
    overrides = overrides or {}
    return {
        "judgments": [
            {
                "item_id": item["item_id"],
                "status": overrides.get(item["source_paths"][0], "matched"),
                "reason": "Frozen semantic judgment",
            }
            for item in build_items(doc, PROFILE, VALIDATION)
        ]
    }


async def content_verdict(gt, prediction, overrides=None):
    return await evaluate(
        gt,
        prediction,
        PROFILE,
        Fake(decisions(gt["intent"]), decisions(prediction["intent"], overrides)),
        MODEL,
        SCHEMA,
        VALIDATION,
    )


@pytest.mark.asyncio
async def test_one_request_three_delegations_penalizes_six_facts_and_replays():
    gt, prediction = artifacts()
    content = await content_verdict(gt, prediction)
    checker = Fake(judgment())
    repetition = await check_repetition(gt, prediction, checker, MODEL)
    final = apply_repetition(content, repetition)
    result = score_record(final)
    assert result["content_scores"]["precision"] == 1
    assert result["precision"] == pytest.approx(1 / 3)
    assert result["recall"] == 1 and result["f1"] == 0.5
    assert result["backward"]["counts"]["extra"] == 6
    assert result["repetition"]["extra_goal_count"] == 2
    assert score_record(json.loads(canonical(final))) == result
    assert score_record(content)["f1"] == 1  # base is never mutated
    assert len(checker.prompts) == 1
    assert '"prediction_intent"' in checker.prompts[0]
    assert '"schema_sha256"' not in checker.prompts[0]  # no GT JSON


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [3, 5])
async def test_explicit_repetition_is_authorized(allowed):
    text = f"Send this email {allowed} times."
    gt, prediction = artifacts(user_text=text)
    content = await content_verdict(gt, prediction)
    repetition = await check_repetition(
        gt, prediction, Fake(judgment(allowed=allowed, user_text=text)), MODEL
    )
    result = score_record(apply_repetition(content, repetition))
    assert result["precision"] == 1 and result["repetition"]["extra_goal_count"] == 0


@pytest.mark.asyncio
async def test_no_group_and_normal_supplementary_messages_preserve_content_score():
    gt, prediction = artifacts()
    response = {
        "checked_goal_paths": goal_paths(prediction["intent"]),
        "groups": [],
        "uncertain_goal_paths": [],
    }
    content = await content_verdict(gt, prediction)
    repetition = await check_repetition(gt, prediction, Fake(response), MODEL)
    result = score_record(apply_repetition(content, repetition))
    assert result["precision"] == result["content_scores"]["precision"] == 1
    assert result["repetition"]["adjustments"] == []


@pytest.mark.asyncio
async def test_changed_extra_ignored_and_uncertain_are_not_charged_again():
    gt, prediction = artifacts()
    prediction["intent"]["entities"] = [{"id": "shared", "entity_type": "person"}]
    prediction["intent"]["goals"][1]["outputs"] = [
        {"entity_ref": "shared", "role": "result"}
    ]
    overrides = {
        "/goals/1/action": "extra",
        "/goals/1/parameters/0": "changed",
        "/goals/1/outputs/0": "ignored",
        "/goals/2/parameters/1": "uncertain",
    }
    content = await content_verdict(gt, prediction, overrides)
    repetition = await check_repetition(gt, prediction, Fake(judgment()), MODEL)
    final = apply_repetition(content, repetition)
    result = score_record(final)
    assert result["precision"] is None and result["recall"] == 1
    rows = {row["source_paths"][0]: row for row in final["backward"]["rows"]}
    assert rows["/entities/0/entity_type"]["status"] == "matched"
    assert all(rows[path]["status"] == status for path, status in overrides.items())
    assert (
        sum(a["original_status"] == "matched" for a in final["repetition_adjustments"])
        == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "api", "budget", "provenance"])
async def test_incomplete_check_never_certifies_precision(failure):
    gt, prediction = artifacts()
    content = await content_verdict(gt, prediction)
    if failure == "provenance":
        prediction["evidence"] = []
    response = judgment(allowed=None) if failure == "unknown" else RuntimeError(failure)
    repetition = await check_repetition(gt, prediction, Fake(response), MODEL)
    result = score_record(apply_repetition(content, repetition))
    assert result["precision"] is result["f1"] is None
    assert result["recall"] == 1 and result["content_scores"]["precision"] == 1
    assert result["status"] == (
        "uncertain"
        if failure == "unknown"
        else "unavailable"
        if failure == "provenance"
        else "error"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["base", "repetition", "rows", "judgments", "mask", "input", "version"]
)
async def test_frozen_records_reject_tampering(mutation):
    gt, prediction = artifacts()
    content = await content_verdict(gt, prediction)
    repetition = await check_repetition(gt, prediction, Fake(judgment()), MODEL)
    final = apply_repetition(content, repetition)
    if mutation == "version":
        final.pop("version")
    elif mutation == "base":
        final["content_evaluation"]["backward"]["rows"][0]["status"] = "extra"
    elif mutation == "repetition":
        final["repetition"]["judgment"]["groups"][0]["allowed_count"] = 3
    elif mutation == "input":
        final["repetition"]["input"]["backend_input"]["text"] = "[]"
    elif mutation == "mask":
        final["repetition_adjustments"] = []
    else:
        target = (
            final["backward"]["rows"]
            if mutation == "rows"
            else final["backward"]["judgments"]["judgments"]
        )
        target[-1]["status"] = "matched"
    with pytest.raises(ValueError):
        score_record(final)


@pytest.mark.asyncio
async def test_coverage_quotes_group_overlap_and_chronology_are_validated():
    gt, prediction = artifacts()
    record = await check_repetition(gt, prediction, Fake(judgment()), MODEL)
    payload = record["input"]
    variants = []
    missing = judgment()
    missing["checked_goal_paths"].pop()
    variants.append(missing)
    overlap = judgment()
    overlap["groups"] *= 2
    variants.append(overlap)
    count = judgment()
    count["groups"][0]["extra_goal_paths"].pop()
    variants.append(count)
    wrong = judgment()
    wrong["groups"][0]["evidence"][0]["quote"] = "Not spoken"
    variants.append(wrong)
    early = judgment()
    early["groups"][0]["extra_goal_paths"] = ["/goals/0", "/goals/1"]
    variants.append(early)
    for invalid in variants:
        with pytest.raises(ValueError):
            validate_judgment(invalid, payload)


@pytest.mark.asyncio
async def test_one_format_repair_and_successful_cache_reuse():
    gt, prediction = artifacts()
    broken = judgment()
    broken["checked_goal_paths"] = []
    model = Fake(broken, judgment())
    result = await check_repetition(gt, prediction, model, MODEL)
    assert result["status"] == "complete" and len(model.prompts) == 2
    cached = await check_repetition(gt, prediction, Fake(), MODEL, result)
    assert cached == result
    # Changed source content invalidates cache instead of applying the old mask.
    gt["input"]["text"] = gt["input"]["text"].replace("15:00", "16:00")
    assert (
        await check_repetition(
            gt, prediction, Fake(RuntimeError("new request")), MODEL, result
        )
    )["status"] == "error"


@pytest.mark.asyncio
async def test_chronology_uses_message_sources_instead_of_goal_array_order():
    gt, prediction = artifacts()
    prediction["evidence"][0]["source_id"] = "b2"
    prediction["evidence"][2]["source_id"] = "b0"
    value = judgment()
    value["groups"][0]["extra_goal_paths"] = ["/goals/0", "/goals/1"]
    repetition = await check_repetition(gt, prediction, Fake(value), MODEL)
    assert repetition["status"] == "complete"
    final = apply_repetition(await content_verdict(gt, prediction), repetition)
    rows = {row["source_paths"][0]: row["status"] for row in final["backward"]["rows"]}
    assert rows["/goals/2/action"] == "matched"
    assert rows["/goals/0/action"] == "extra"


def test_nested_goals_source_validation_and_json_escaped_quotes():
    doc = {
        "goals": [],
        "rules": [{"then_goals": [{"id": "g", "action": "send_email"}]}],
    }
    path = "/rules/0/then_goals/0"
    text = canonical(
        [
            {
                "source_id": "b",
                "content": [{"text": json.dumps({"objective": 'Send "hello"\nnow'})}],
            }
        ]
    )
    evidence = [{"path": path, "source_id": "b", "quote": 'Send "hello"\nnow'}]
    validate_goal_evidence(doc, evidence, text)
    assert goal_paths(doc) == [path]
    with pytest.raises(ValueError, match="cover every"):
        validate_goal_evidence(doc, [], text)


@pytest.mark.asyncio
async def test_prediction_prompt_and_goal_sources_are_independent_of_gt():
    gt, prediction = artifacts(1)
    record = TextRecord(
        "call", prediction["input"]["text"], context={"require_goal_sources": True}
    )
    model = Fake(
        {
            "intent": prediction["intent"],
            "evidence": [
                item | {"path": "/intent" + item["path"]}
                for item in prediction["evidence"]
            ],
        }
    )
    result = await extract(
        record,
        SCHEMA,
        DEFINITION.instructions(),
        PROFILE,
        model,
        MODEL,
        "prediction",
        VALIDATION,
    )
    assert result["status"] == "complete"
    assert result["evidence"][0]["path"] == "/goals/0"
    assert gt["input"]["text"] not in model.prompts[0]


@pytest.mark.asyncio
async def test_whole_call_pipeline_resume_and_empty_prediction(monkeypatch, tmp_path):
    from tau2.data_model.diagnostics import BackendInput, DiagnosticEvidence, UserInput
    from tau2.diagnostics.intent import call

    evidence = DiagnosticEvidence(
        simulation_id="call",
        task_id="task",
        domain="gaia2",
        backend_status="complete",
        user_inputs=[UserInput(id="u", text=TEXT, started=0, ended=1)],
        backend_inputs=[
            BackendInput(
                id=f"b{i}", content=TEXT, time=i + 2, task_id=f"t{i}", accepted=True
            )
            for i in range(3)
        ],
    )
    seen = []
    fail_check = True

    class Model:
        def __init__(self, config, role, *args):
            self.role = role

        async def complete(self, prompt, schema):
            seen.append((self.role, prompt))
            if self.role == "gt":
                return json.dumps({"intent": document(1)})
            if self.role == "prediction":
                return json.dumps(
                    {"intent": document(), "evidence": artifacts()[1]["evidence"]}
                )
            if "checked_goal_paths" in schema.get("properties", {}):
                if fail_check:
                    raise RuntimeError("grading request budget exhausted")
                return json.dumps(judgment())
            paths = schema["properties"]["judgments"]
            assert paths
            doc = document(1) if "Forward:" in prompt else document()
            return json.dumps(decisions(doc))

    monkeypatch.setattr(call, "TauJsonModel", Model)
    failed = await evaluate_call(evidence, MetricsConfig(), tmp_path, RequestBudget(10))
    assert failed["precision"] is None and failed["recall"] == 1
    assert len(seen) == 5
    fail_check = False
    result = await evaluate_call(evidence, MetricsConfig(), tmp_path, RequestBudget(10))
    assert result["version"] == "tau-real-intent/2" and result["f1"] == 0.5
    assert len(seen) == 6
    assert {p.name for p in tmp_path.glob("*.json")} == {
        "gt.json",
        "prediction.json",
        "evaluation.content.json",
        "repetition.json",
        "evaluation.json",
        "scores.json",
    }
    assert (
        await evaluate_call(evidence, MetricsConfig(), tmp_path, RequestBudget(10))
        == result
    )
    assert len(seen) == 6
    _, backend, _ = combined_sources(evidence)
    assert json.loads(backend.text)[0]["task_id"] == "t0"
    assert "merge goals across independent delegations" in seen[1][1]
    evidence.backend_inputs = []
    empty = await evaluate_call(
        evidence, MetricsConfig(), tmp_path / "empty", RequestBudget(10)
    )
    assert empty["precision"] == 1 and empty["f1"] == 0
    assert len(seen) == 7


@pytest.mark.asyncio
async def test_failed_repetition_resumes_without_rerunning_content(
    monkeypatch, tmp_path
):
    # Successful request cache is covered separately; only the failed check retries.
    gt, prediction = artifacts()
    content = await content_verdict(gt, prediction)
    failed = await check_repetition(gt, prediction, Fake(RuntimeError("budget")), MODEL)
    write_json(tmp_path / "evaluation.content.json", content)
    fixed = await check_repetition(gt, prediction, Fake(judgment()), MODEL, failed)
    assert fixed["status"] == "complete"
    assert score_record(apply_repetition(content, fixed))["precision"] == pytest.approx(
        1 / 3
    )


@pytest.mark.asyncio
async def test_conditional_inline_and_cross_goal_facts_keep_their_scopes():
    gt, prediction = artifacts()
    goals = prediction["intent"].pop("goals")
    for goal in goals:
        goal["target"] = {"kind": "selection", "selection": {"entity_type": "email"}}
    prediction["intent"] |= {
        "goals": [],
        "rules": [
            {
                "id": "r",
                "condition": {"kind": "predicate", "name": "approved", "arguments": []},
                "then_goals": goals,
            }
        ],
        "relations": [
            {
                "type": "same_context",
                "arguments": [
                    {"name": "first", "value": {"kind": "goal_ref", "ref": "g0"}},
                    {"name": "second", "value": {"kind": "goal_ref", "ref": "g1"}},
                ],
            }
        ],
    }
    for annotation in prediction["evidence"]:
        annotation["path"] = annotation["path"].replace(
            "/goals/", "/rules/0/then_goals/"
        )
    response = judgment()
    response["checked_goal_paths"] = goal_paths(prediction["intent"])
    response["groups"][0]["goal_paths"] = goal_paths(prediction["intent"])
    response["groups"][0]["extra_goal_paths"] = goal_paths(prediction["intent"])[1:]
    content = await content_verdict(gt, prediction)
    repetition = await check_repetition(gt, prediction, Fake(response), MODEL)
    final = apply_repetition(content, repetition)
    assert final["status"] == "complete"
    for row in final["backward"]["rows"]:
        path = row["source_paths"][0]
        if path.startswith(("/relations/", "/rules/0/condition/")):
            assert row["status"] == "matched"
        elif path.startswith("/rules/0/then_goals/1/"):
            assert row["status"] == "extra"


@pytest.mark.asyncio
async def test_summary_and_terminal_show_counts_evidence_and_versions(monkeypatch):
    from io import StringIO

    from rich.console import Console

    from tau2.data_model.simulation import SimulationRun, TerminationReason
    from tau2.diagnostics.suite import summarize
    from tau2.utils.display import ConsoleDisplay

    gt, prediction = artifacts()
    content = await content_verdict(gt, prediction)
    record = await check_repetition(gt, prediction, Fake(judgment()), MODEL)
    result = score_record(apply_repetition(content, record))
    row = {"profile": "same", "summary": {"measures": {"intent": result}}}
    report = summarize([row])
    assert report["intent"]["micro"]["precision"] == pytest.approx(1 / 3)
    assert report["intent"]["repetition"]["extra_goal_count"] == 2
    assert report["intent"]["repetition"]["coverage"] == 1
    old = deepcopy(row)
    old["summary"]["measures"]["intent"]["version"] = "tau-real-intent/1"
    assert "intent" not in summarize([row, old])
    output = StringIO()
    monkeypatch.setattr(ConsoleDisplay, "console", Console(file=output, width=160))
    simulation = SimulationRun(
        id="s",
        task_id="t",
        trial=0,
        start_time="start",
        end_time="end",
        duration=1,
        messages=[],
        termination_reason=TerminationReason.USER_STOP,
        diagnostics_ref=row,
    )
    ConsoleDisplay.display_simulation(simulation)
    text = output.getvalue()
    assert "extra goals: 2" in text and "Content precision: 1" in text
    assert "user u:" in text and "matched -> extra" in text

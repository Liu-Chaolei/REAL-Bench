"""No oracle dialogue reaches planners; audits and work versions remain traceable."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_task_authoring.test_gaia2_series import (  # noqa: F401
    first_stage,
    make_base,
    native_source,
)

from tau2.task_authoring.gaia2_series import generation, questions, revalidation
from tau2.task_authoring.gaia2_series.questions import (
    QuestionAudit,
    QuestionMaterialReader,
    QuestionPlan,
    effective_verdict,
)
from tau2.task_authoring.gaia2_series.schema import AuthoringConfig, ModelProfile
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    read_record,
    record_path,
    write_json,
    write_jsonl,
)


def decision(verdict="pass", **changes):
    result = []
    for stage in [1, 2]:
        row = dict(
            stage=stage,
            verdict=verdict,
            grounded=True,
            clear_target=True,
            substantive=True,
            independent_of_agent_reply=True,
            direct_completion_ok=True,
            different_wording_ok=True,
            different_clarification_ok=True,
            already_done_adds_goal=True,
            reason="The request adds an independent business goal.",
            **{},
        )
        row.update(changes)
        result.append(row)
    return QuestionAudit(stages=result)


@pytest.fixture
def safe_reader(tmp_path, monkeypatch):
    base = make_base()
    base.evaluation_criteria.communicate_info = ["ORACLE_ANSWER_CANARY"]
    base.scenario.events[-1].arguments["content"] = "ORACLE_REPLY_CANARY"
    (tmp_path / "indexes").mkdir()
    write_json(
        tmp_path / "indexes" / f"{base.scenario.snapshot}.json",
        {
            "schemas": {
                "Contacts__get_contact": {"type": "function"},
                "AgentUserInterface__get_messages": {},
            }
        },
    )
    state = {
        "Contacts": {
            "contacts": {
                "self": {"name": "Alice", "note": "A business note mentioning Thanks"}
            }
        },
        "AgentUserInterface": {"messages": ["ORACLE_SNAPSHOT_CANARY"]},
        "System": {"notifications": ["ORACLE_SYSTEM_CANARY"]},
    }
    monkeypatch.setattr(questions, "_initial", lambda *args: copy.deepcopy(state))
    return tmp_path, base, state


def test_all_planner_inputs_exclude_oracle_fields(safe_reader):
    dataset, base, _ = safe_reader
    reader = QuestionMaterialReader(dataset, base, "unused")
    payload = json.dumps(reader.packet())
    assert (
        "CANARY" not in payload
        and "evaluation_criteria" not in payload
        and "scenario" not in payload
    )
    assert "AgentUserInterface" not in payload
    assert "business note" in reader.get_material("/Contacts/contacts/self/note")
    assert "business note" in reader.search_material("Thanks")
    assert "CANARY" not in reader.search_material("ORACLE")
    for source in ["result", "state", "answer", "journal"]:
        with pytest.raises(ValueError):
            reader.get_material(source=source)
    with pytest.raises(KeyError):
        reader.get_material("/AgentUserInterface/messages")
    with pytest.raises(ValueError):
        reader.read_public_tool("AgentUserInterface__get_messages", {})


def test_request_planning_and_blind_audit_omit_old_rationale(safe_reader, monkeypatch):
    dataset, base, _ = safe_reader
    capture = []
    texts = [
        "Please create a profile review reminder.",
        "Now send Alice a reminder about that requested event.",
    ]
    plan = QuestionPlan(
        requests=[
            dict(
                request=t,
                new_goal="A substantive related new goal",
                business_relation="Uses the previously requested event",
                business_objects=["requested event"],
                sources=[{"source": "original_request"}],
            )
            for t in texts
        ]
    )

    def call(config, profile, rules, payload, response_model, *args, **kwargs):
        capture.append(payload)
        return plan if response_model is QuestionPlan else decision()

    monkeypatch.setattr(generation, "call_model", call)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    result = questions.plan_questions(
        dataset,
        base,
        "unused",
        config,
        [],
        failure_categories=["agent_reply_dependency"],
    )
    audit = questions.audit_questions(dataset, base, "unused", texts, config, [])
    assert audit["verdict"] == "pass" and result.requests[0].request == texts[0]
    for payload in capture:
        assert "CANARY" not in json.dumps(payload)
        assert "reference_results" not in payload and "relation" not in payload


def test_contradictory_pass_and_invented_quotes_cannot_pass():
    assert (
        effective_verdict(decision(independent_of_agent_reply=False), ["a", "b"])
        == "uncertain"
    )
    assert effective_verdict(decision(quote="invented"), ["a", "b"]) == "uncertain"
    assert (
        effective_verdict(
            decision(), ["Thanks, do a new task.", "Now process that order."]
        )
        == "pass"
    )


def test_uncertain_audit_gets_at_most_one_unanchored_adjudication(
    safe_reader, monkeypatch
):
    dataset, base, _ = safe_reader
    packets = []

    def call(*args, **kwargs):
        packets.append(args[3])
        return decision("uncertain")

    monkeypatch.setattr(generation, "call_model", call)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    audited = questions.audit_questions(
        dataset, base, "unused", ["First request", "Second request"], config, []
    )
    assert audited["verdict"] == "uncertain" and len(packets) == 2
    assert packets[0] == packets[1] and "previous_verdict" not in packets[1]


def test_queue_union_is_deduplicated_and_pending_never_counts_as_pass(tmp_path):
    write_json(
        tmp_path / "population.json",
        {
            "expected_tasks": 4,
            "legacy_reviewed": ["a", "b", "c"],
            "legacy_unresolved": ["d"],
        },
    )
    for k, v in [("a", "pass"), ("b", "fail"), ("c", "error")]:
        write_json(tmp_path / "question-audits" / f"{digest(k)}.json", {"verdict": v})
    first = revalidation.build_queue(tmp_path)
    second = revalidation.build_queue(tmp_path)
    assert first == second and set(first) == {"b", "c", "d"}
    assert first["c"]["route"] == "semantic_blocked"


def test_concurrent_reports_publish_valid_json_without_temp_name_collisions(tmp_path):
    def worker(i):
        write_json(tmp_path / "report.json", {"worker": i})
        write_jsonl(tmp_path / "report.jsonl", [{"worker": i}])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(40)))
    assert "worker" in json.loads((tmp_path / "report.json").read_text())
    assert "worker" in json.loads((tmp_path / "report.jsonl").read_text())


def test_solver_cannot_change_a_locked_user_request(monkeypatch):
    base = make_base()
    from tau2.task_authoring.gaia2_series.schema import SeriesDraft

    monkeypatch.setattr(
        generation, "MaterialReader", lambda *args: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(generation, "call_model", lambda *args, **kwargs: first_stage())
    token = questions.locked_requests.set(
        ["A different locked user request.", "The second fixed request."]
    )
    try:
        config = AuthoringConfig(
            author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
        )
        with pytest.raises(ValueError, match="locked"):
            generation.author_stage(
                config,
                base,
                None,
                SimpleNamespace(results={}, checkpoints=[]),
                {},
                SeriesDraft(base_task_id=base.id, base_task_sha256=digest(base)),
                "",
                [],
            )
    finally:
        questions.locked_requests.reset(token)


def test_legacy_reviewed_is_not_new_policy_complete(tmp_path):
    write_json(
        tmp_path / "population.json", {"expected_tasks": 1, "records": {"a": {}}}
    )
    write_json(record_path(tmp_path, "a"), {"base_task_id": "a", "status": "reviewed"})
    result = revalidation.coverage(tmp_path)
    assert (
        result["accepted"] == 0
        and result["complete"] is False
        and result["pending"] == 1
    )


@pytest.mark.parametrize("legacy_status", ["reviewed", "needs_revision"])
def test_new_version_rebuilds_or_revalidates_without_touching_legacy(
    native_source,  # noqa: F811
    monkeypatch,
    legacy_status,
):
    from test_task_authoring.test_gaia2_series import complete_fixture, second_stage

    from tau2.task_authoring.gaia2_series.schema import ReviewDecision

    task, source, legacy = native_source
    candidate = complete_fixture(task, source)
    original = {
        "base_task_id": task.id,
        "status": legacy_status,
        "draft": candidate.model_dump(mode="json")
        if legacy_status == "reviewed"
        else None,
        "attempts": {"1": 3},
        "repair_attempts": {"1": 3},
    }
    write_json(record_path(legacy, task.id), original)
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/fixture"),
        reviewer=ModelProfile(model="mimo/fixture"),
    )
    output = legacy.parent / "revalidated"
    pop = revalidation.prepare_version(legacy, output, config)
    assert len(pop["records"]) == 1

    def audit(dataset, base, root, requests, *args, **kwargs):
        return {
            "policy_version": questions.POLICY_VERSION,
            "verdict": "pass",
            "requests_sha256": digest(requests),
            "affected_stages": [],
        }

    texts = [s.request for s in candidate.stages]
    plan = QuestionPlan(
        requests=[
            dict(
                request=t,
                new_goal="An independently added business goal",
                business_relation="Continues a previously requested object",
                business_objects=["requested reminder"],
                sources=[{"source": "original_request"}],
            )
            for t in texts
        ]
    )
    monkeypatch.setattr(revalidation, "audit_questions", audit)
    monkeypatch.setattr(revalidation, "plan_questions", lambda *args, **kwargs: plan)

    def author(config, base, source, execution, context, draft, feedback, calls):
        return (
            first_stage()
            if not draft.stages
            else second_stage(execution.results["series_s1_create"])
        )

    monkeypatch.setattr(generation, "author_stage", author)
    monkeypatch.setattr(
        generation,
        "review_series",
        lambda *args: ReviewDecision(
            **{
                k: True
                for k, v in ReviewDecision.model_fields.items()
                if v.annotation is bool
            }
        ),
    )
    result = revalidation.rebuild_one(output, config, task.id)
    assert result["status"] == "accepted", result
    assert revalidation.coverage(output)["accepted"] == 1
    assert read_record(legacy, task.id) == original
    record = read_record(output, task.id)
    assert record["legacy_acceptance"]["status"] == legacy_status
    assert record["attempts"] == original["attempts"]
    assert record["repair_attempts"] == original["repair_attempts"]
    record["draft"]["stages"][0]["request"] += " Changed after approval."
    assert not revalidation.policy_complete(record)


def test_calibration_requires_each_negative_stage_to_fail(native_source, monkeypatch):  # noqa: F811
    task, _, dataset = native_source
    monkeypatch.setattr(revalidation, "PILOT_ID", task.id)

    def audit(dataset, base, source, texts, *args, **kwargs):
        verdict = decision()
        if texts[0].startswith("Okay"):
            verdict.stages[1].verdict = "fail"
            verdict.stages[1].substantive = False
            verdict.stages[1].independent_of_agent_reply = False
        overall = "fail" if texts[0].startswith("Okay") else "pass"
        return {
            "verdict": overall,
            "attempts": [{"decision": verdict.model_dump(mode="json")}],
        }

    monkeypatch.setattr(revalidation, "audit_questions", audit)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    result = revalidation.calibrate_questions(dataset, config)
    assert not result["passed"]
    assert result["cases"][-1]["audit"]["verdict"] == "fail"
    assert result["cases"][-1]["passed"] is False

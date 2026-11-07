"""Grounded series authoring: native fixtures, real tools, and no live model calls."""

import gzip
import json

import pytest

from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.data_model.tasks import (
    EnvAssertion,
    EvaluationCriteria,
    RewardType,
    StructuredUserInstructions,
    Task,
    UserScenario,
)
from tau2.task_authoring.gaia2_series.compiler import compile_series, validate_graph
from tau2.task_authoring.gaia2_series.materials import pointer_get
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    Evidence,
    ModelProfile,
    OutcomeCheck,
    ReviewDecision,
    SeriesDraft,
    StageDraft,
    ValueRef,
)
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    file_digest,
    read_record,
    record_path,
    stratified_ids,
    write_json,
)


@pytest.mark.parametrize(
    "completed,elapsed,errors,infra,expected",
    [
        (49, 1801, 0, 0, 8),
        (50, 1799, 0, 0, 8),
        (50, 1800, 0, 0, 8),
        (100, 3600, 6, 0, 8),
        (100, 3600, 0, 1, 8),
    ],
)
def test_full_concurrency_never_uses_task_statistics(
    completed, elapsed, errors, infra, expected
):
    from tau2.task_authoring.gaia2_series.full import concurrency_for

    calls = [
        {
            "elapsed_seconds": 10,
            "timeout_seconds": 600,
            "error": "failed" if index < errors else None,
        }
        for index in range(100)
    ]
    assert (
        concurrency_for(
            8,
            16,
            completed=completed,
            elapsed=elapsed,
            calls=calls,
            infrastructure_errors=infra,
        )
        == expected
    )


def test_full_anomaly_gate_retains_repaired_history(native_source):
    from tau2.task_authoring.gaia2_series import pipeline

    task, _, dataset = native_source
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "status": "reviewed",
            "calls": [{"provider_attempt": 2}],
        },
    )
    assert task.id in pipeline.human_review_ids(
        dataset, {"human_review_ids": ["pilot"]}, {task.id: task}
    )
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "status": "reviewed",
            "review": {
                "accepted": True,
                "decision": {"reasons": ["The task is grounded in materials."]},
            },
        },
    )
    assert not pipeline.anomaly_ids(dataset, {task.id: task})


@pytest.mark.parametrize("provider_failure", [False, True])
def test_full_clean_migration_resume_and_human_gate(
    native_source, monkeypatch, provider_failure
):
    from tau2.task_authoring.gaia2_series import full, generation, pipeline
    from tau2.task_authoring.gaia2_series.schema import RepairResult

    task, source, pilot = native_source
    second = task.model_copy(deep=True)
    second.id = "second"
    write_json(
        source / "tasks.json",
        [task.model_dump(mode="json"), second.model_dump(mode="json")],
    )
    source_manifest = json.loads((source / "manifest.json").read_text())
    source_manifest["tasks_sha256"] = file_digest(source / "tasks.json")
    write_json(source / "manifest.json", source_manifest)
    manifest = json.loads((pilot / "manifest.json").read_text())
    manifest["expected_tasks"] = 2
    manifest["source_files"]["tasks.json"] = file_digest(source / "tasks.json")
    manifest["source_files"]["manifest.json"] = file_digest(source / "manifest.json")
    write_json(pilot / "manifest.json", manifest)
    (pilot / "inventory.jsonl").write_text(json.dumps({"id": task.id}) + "\n")
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    generated = []
    provider_failed = []

    def author(config, base, root, execution, context, prefix, feedback, calls):
        generated.append(base.id)
        if provider_failure and base.id == second.id and not provider_failed:
            provider_failed.append(True)
            raise generation.ProviderUnavailable("Transient connection failure")
        return (
            first_stage()
            if not prefix.stages
            else second_stage(execution.results["series_s1_create"])
        )

    def reviewer(*args):
        return ReviewDecision(
            **{
                key: True
                for key, field in ReviewDecision.model_fields.items()
                if field.annotation is bool
            }
        )

    monkeypatch.setattr(generation, "author_stage", author)
    monkeypatch.setattr(generation, "review_series", reviewer)
    monkeypatch.setattr(
        generation,
        "repair_stage",
        lambda *args: RepairResult(stage=first_stage(), reason="fixture"),
    )
    pipeline.generate_dataset(pilot, config, [task.id])
    pipeline.review_dataset(pilot, config, [task.id])
    approved = read_record(pilot, task.id)
    write_json(
        record_path(pilot, "stale"), {"base_task_id": "stale", "status": "generating"}
    )
    decisions = pilot / "approvals.jsonl"
    decisions.write_text(
        json.dumps(
            {
                "base_task_id": task.id,
                "content_sha256": digest(approved["draft"]),
                "reviewer": "fixture-user",
                "decision": "accepted",
            }
        )
        + "\n"
    )
    output = pilot.parent / "full"
    result = full.prepare_dataset(pilot, output, decisions)
    assert result["remaining"] == 1
    assert len(list((output / "work").glob("*.json"))) == 1
    assert not record_path(output, "stale").exists()
    generated.clear()
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy

    policy = RequestPolicy(adaptive_concurrency=False)
    monkeypatch.setattr(
        full,
        "concurrency_for",
        lambda *args, **kwargs: pytest.fail(
            "Fixed mode must not evaluate adaptive scaling"
        ),
    )
    result = full.run_full(
        output, config, start_concurrency=2, max_concurrency=4, request_policy=policy
    )
    assert result["status"] == "awaiting_human_review"
    assert generated == [second.id] * (3 if provider_failure else 2)
    if provider_failure:
        assert second.id in pipeline.anomaly_ids(
            output, {task.id: task, second.id: second}
        )
    assert read_record(output, task.id)["human"]["decision"] == "accepted"
    assert not (output / "release").exists()
    assert (
        json.loads((output / "coverage.json").read_text())["automatically_accepted"]
        == 2
    )
    generated.clear()
    full.run_full(
        output, config, start_concurrency=2, max_concurrency=4, request_policy=policy
    )
    assert not generated


@pytest.mark.parametrize(
    "operation,actual,expected",
    [
        ("contains", {"event": {"title": "Meeting"}}, {"title": "Meeting"}),
        ("sum", {"price": "wrong type"}, 1),
        ("set_equals", None, []),
    ],
)
def test_bad_check_types_are_repairable_candidate_errors(operation, actual, expected):
    from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
    from tau2.task_authoring.gaia2_series.verification import Execution, evaluate_check

    execution = Execution({1: {"value": actual}}, {}, [], [], {}, {})
    check = OutcomeCheck(
        reference=ValueRef(source="state", stage=1, pointer="/value"),
        operation=operation,
        expected=expected,
        explanation="Check the requested outcome",
    )
    with pytest.raises(CandidateValidationError) as caught:
        evaluate_check(check, execution)
    assert caught.value.details["failure_class"] == "candidate"
    assert caught.value.details["check"]["operation"] == operation
    assert "actual" in caught.value.details


def test_environment_start_failure_remains_system_error(native_source, monkeypatch):
    from tau2.domains.gaia2 import environment
    from tau2.task_authoring.gaia2_series.failures import (
        VerificationInfrastructureError,
    )
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source

    def broken(**kwargs):
        raise OSError("Cannot initialize resource sandbox")

    monkeypatch.setattr(environment, "get_environment", broken)
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[first_stage()]
    )
    with pytest.raises(VerificationInfrastructureError, match="environment_startup"):
        verify_draft(task, source, draft)


def test_saved_misclassified_candidate_recovers_without_resetting_budget(native_source):
    from tau2.task_authoring.gaia2_series.recovery import recover_candidates

    task, source, dataset = native_source
    draft = complete_fixture(task, source)
    candidate = draft.stages.pop()
    candidate.checks.append(
        OutcomeCheck(
            reference=ValueRef(source="state", stage=2, pointer="/Calendar/events"),
            operation="contains",
            expected={"title": "Profile review"},
            explanation="The rescheduled reminder must exist",
        )
    )
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "draft": draft.model_dump(mode="json"),
            "status": "verification_error",
            "last_error": "unhashable type: 'dict'",
            "attempts": {"1": 1, "2": 2},
            "repair_attempts": {"2": 1},
            "calls": [
                {"purpose": "StageDraft", "content": candidate.model_dump_json()}
            ],
        },
    )
    result = recover_candidates(dataset, [task.id])
    record = read_record(dataset, task.id)
    assert result[0]["status"] == "needs_revision"
    assert record["failure_class"] == "candidate"
    assert record["attempts"] == {"1": 1, "2": 3}
    assert record["repair_attempts"] == {"2": 1}
    assert record["draft"] == draft.model_dump(mode="json")
    assert record["candidate_history"][-1]["candidate"] == candidate.model_dump(
        mode="json"
    )
    assert recover_candidates(dataset, [task.id])[0]["status"] == "unchanged"


def make_base(snapshot="a" * 64):
    """Use native structures even for schema-only tests without Gaia2 extras."""
    return Task(
        id="fixture",
        scenario=ScenarioSpec(
            start_time=1728975600,
            source_id="fixture",
            source_sha256="b" * 64,
            snapshot=snapshot,
            category="execution",
            events=[
                ScenarioAction(
                    id="u",
                    kind="user",
                    tool="input",
                    arguments={"content": "Update my age to 31."},
                ),
                ScenarioAction(
                    id="edit",
                    kind="reference",
                    tool="Contacts__edit_contact",
                    arguments={"contact_id": "self", "updates": {"age": 31}},
                    dependencies=["u"],
                ),
            ],
        ),
        user_scenario=UserScenario(
            instructions=StructuredUserInstructions(
                domain="gaia2",
                reason_for_call="Update my age to 31.",
                task_instructions="Do not invent new objectives.",
            )
        ),
        evaluation_criteria=EvaluationCriteria(
            env_assertions=[
                EnvAssertion(
                    env_type="assistant",
                    func_name="assert_scenario_goal",
                    arguments={"source_id": "fixture"},
                )
            ],
            reward_basis=[RewardType.ENV_ASSERTION],
        ),
    )


def first_stage():
    """Add a reminder related to the completed profile update."""
    return StageDraft(
        request="Create a calendar reminder titled Profile review tomorrow at noon for one hour.",
        relation="The reminder concerns the profile just updated in the original request.",
        evidence=[
            Evidence(
                reference=ValueRef(
                    source="state", stage=0, pointer="/Contacts/contacts/self/age"
                ),
                expected=31,
                explanation="The original profile update completed.",
                public_tool="Contacts__get_contact",
                public_arguments={"contact_id": "self"},
                result_pointer="/age",
            )
        ],
        events=[
            ScenarioAction(
                id="series_s1_create",
                kind="reference",
                tool="Calendar__add_calendar_event",
                arguments={
                    "title": "Profile review",
                    "start_datetime": "2024-10-16 12:00:00",
                    "end_datetime": "2024-10-16 13:00:00",
                },
            )
        ],
        criteria=EvaluationCriteria(
            reward_basis=[RewardType.ENV_ASSERTION],
            nl_assertions=["Create exactly the requested reminder."],
        ),
        checks=[
            OutcomeCheck(
                reference=ValueRef(source="state", stage=1, pointer="/Calendar/events"),
                operation="count",
                expected=1,
                explanation="One reminder was created.",
            )
        ],
    )


def second_stage(identifier):
    """Revise the generated object using a scalar reference instead of a baked-in ID."""
    return StageDraft(
        request="Move that reminder to 2 PM on the same day and keep its duration and title.",
        relation="The reminder created in the previous stage must be rescheduled.",
        evidence=[
            Evidence(
                reference=ValueRef(
                    source="result", stage=1, event_id="series_s1_create"
                ),
                expected=identifier,
                explanation="The previously created reminder exists.",
                public_tool="Calendar__get_calendar_event",
                public_arguments={"event_id": "{{series_s1_create}}"},
                result_pointer="/event_id",
            )
        ],
        events=[
            ScenarioAction(
                id="series_s2_delete",
                kind="reference",
                tool="Calendar__delete_calendar_event",
                arguments={"event_id": "{{series_s1_create}}"},
            ),
            ScenarioAction(
                id="series_s2_create",
                kind="reference",
                tool="Calendar__add_calendar_event",
                arguments={
                    "title": "Profile review",
                    "start_datetime": "2024-10-16 14:00:00",
                    "end_datetime": "2024-10-16 15:00:00",
                },
                dependencies=["series_s2_delete"],
            ),
            ScenarioAction(
                id="series_s2_read",
                kind="reference",
                tool="Calendar__get_calendar_event",
                arguments={"event_id": "{{series_s2_create}}"},
                dependencies=["series_s2_create"],
            ),
        ],
        criteria=EvaluationCriteria(
            reward_basis=[RewardType.ENV_ASSERTION],
            nl_assertions=["Keep the same title and one-hour duration."],
        ),
        checks=[
            OutcomeCheck(
                reference=ValueRef(source="state", stage=2, pointer="/Calendar/events"),
                operation="count",
                expected=1,
                explanation="No obsolete or duplicate reminder remains.",
            ),
            OutcomeCheck(
                reference=ValueRef(
                    source="result",
                    stage=2,
                    event_id="series_s2_read",
                    pointer="/title",
                ),
                expected="Profile review",
                explanation="The reminder retains its title.",
            ),
        ],
    )


@pytest.fixture
def native_source(tmp_path):
    """Build sealed source identities with actual isolated Gaia2 applications."""
    for dependency in [
        "termcolor",
        "fsspec",
        "rapidfuzz",
        "puremagic",
        "mammoth",
        "markdownify",
        "pdfminer",
        "pptx",
        "bs4",
    ]:
        pytest.importorskip(dependency)
    from tau2.domains.gaia2.tools import Gaia2Tools
    from tau2.domains.gaia2.utils import canonical_json, runtime_digest
    from tau2.task_authoring.gaia2_series.materials import (
        app_states,
        index_records,
        public_schema,
    )

    snapshot = [
        {
            "name": "Contacts",
            "class_name": "Contacts",
            "app_state": {
                "contacts": {
                    "self": {
                        "contact_id": "self",
                        "first_name": "Alice",
                        "last_name": "Example",
                        "is_user": True,
                        "age": 30,
                        "email": "alice@example.org",
                    }
                },
                "view_limit": 10,
            },
        },
        {"name": "Calendar", "class_name": "Calendar", "app_state": {"events": {}}},
    ]
    root, dataset = tmp_path / "source", tmp_path / "series"
    (root / "snapshots").mkdir(parents=True)
    assets = root / "assets"
    assets.mkdir()
    raw = canonical_json(snapshot)
    snapshot_id = digest(snapshot)
    (root / "snapshots" / f"{snapshot_id}.json.gz").write_bytes(
        gzip.compress(raw, mtime=0)
    )
    task = make_base(snapshot_id)
    task.scenario.runtime_sha256 = runtime_digest()
    task.scenario.assets_sha256 = digest({})
    (root / "tasks.json").write_bytes(canonical_json([task.model_dump(mode="json")]))
    write_json(root / "resources.json", {})
    write_json(root / "split_tasks.json", {"base": [task.id]})
    write_json(
        root / "manifest.json",
        {
            "verified": True,
            "assets": str(assets),
            "runtime_sha256": runtime_digest(),
            "assets_sha256": digest({}),
            "tasks_sha256": file_digest(root / "tasks.json"),
        },
    )
    dataset.mkdir()
    write_json(
        dataset / "manifest.json",
        {
            "version": "gaia2-series-v1",
            "source_root": str(root),
            "expected_tasks": 1,
            "runtime_sha256": runtime_digest(),
            "source_files": {
                name: file_digest(root / name)
                for name in [
                    "tasks.json",
                    "resources.json",
                    "manifest.json",
                    "split_tasks.json",
                ]
            },
            "pilot_ids": [],
            "additional_human_ids": [],
            "seed": 300,
            "status": "authoring",
        },
    )
    toolkit = Gaia2Tools(snapshot, assets=assets)
    try:
        schemas = public_schema(toolkit)
    finally:
        toolkit.close()
    write_json(
        dataset / "indexes" / f"{snapshot_id}.json",
        {"schemas": schemas, "records": list(index_records(app_states(snapshot)))},
    )
    return task, root, dataset


def test_compile_preserves_original_and_two_stage_prefix():
    base = make_base()
    original = base.model_dump(mode="json")
    draft = SeriesDraft(
        base_task_id=base.id,
        base_task_sha256=digest(base),
        stages=[first_stage(), second_stage("some-id")],
    )
    long, index = compile_series(base, draft)
    short = compile_series(base, draft.model_copy(update={"stages": draft.stages[:1]}))[
        0
    ]
    assert base.model_dump(mode="json") == original
    assert long.scenario.events[: len(base.scenario.events)] == base.scenario.events
    assert long.scenario.events[: len(short.scenario.events)] == short.scenario.events
    assert (
        long.user_scenario.instructions.reason_for_call
        == base.user_scenario.instructions.reason_for_call
    )
    assert index[0]["criteria"] == base.evaluation_criteria.model_dump(mode="json")


def test_existing_clarification_stays_in_original_stage():
    base = make_base()
    base.scenario.events.append(
        ScenarioAction(
            id="clarify",
            kind="user",
            tool="input",
            arguments={"content": "Yes, that profile."},
            dependencies=["edit"],
        )
    )
    task, index = compile_series(
        base,
        SeriesDraft(
            base_task_id=base.id, base_task_sha256=digest(base), stages=[first_stage()]
        ),
    )
    assert "clarify" in index[0]["event_ids"]
    assert next(
        event for event in task.scenario.events if event.id == "series_s1_user"
    ).dependencies == ["clarify"]


@pytest.mark.parametrize(
    "mutation",
    ["private_kind", "future_evidence", "leaked_id", "bad_hash", "bad_reward"],
)
def test_invalid_authoring_contracts_are_rejected(mutation):
    base = make_base()
    draft = SeriesDraft(
        base_task_id=base.id, base_task_sha256=digest(base), stages=[first_stage()]
    )
    if mutation == "private_kind":
        draft.stages[0].events[0].kind = "environment"
    elif mutation == "future_evidence":
        draft.stages[0].evidence[0].reference.stage = 1
    elif mutation == "leaked_id":
        draft.stages[0].request += " " + "a" * 32
    elif mutation == "bad_reward":
        draft.stages[0].criteria.reward_basis = [RewardType.DB]
    else:
        draft.base_task_sha256 = "wrong"
    with pytest.raises(ValueError):
        compile_series(base, draft)


def test_graph_and_pointer_fail_closed():
    assert pointer_get({"a/b": {"~": [3]}}, "/a~1b/~0/0") == 3
    with pytest.raises(KeyError):
        pointer_get({}, "/missing")
    for events in [
        [ScenarioAction(id="x", kind="reference", tool="t", dependencies=["missing"])],
        [
            ScenarioAction(
                id="x", kind="reference", tool="t", arguments={"id": "{{unknown}}"}
            )
        ],
    ]:
        with pytest.raises(ValueError):
            validate_graph(events)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pointer", "/Chats/conversations/{{series_s1_create}}/title"),
        ("event_id", "{{series_s1_create}}"),
        ("pointer", "/bad~escape"),
    ],
)
def test_value_references_reject_templates_and_invalid_escaping(field, value):
    with pytest.raises(ValueError):
        ValueRef.model_validate(
            {"source": "result", "event_id": "series_s1_create", field: value}
        )


@pytest.mark.parametrize(
    "location", ["reference", "public_result", "compare_to", "event_id", "dependency"]
)
def test_bad_reference_format_fails_before_environment_creation(location, monkeypatch):
    from pathlib import Path

    from tau2.domains.gaia2 import environment, utils
    from tau2.task_authoring.gaia2_series import verification
    from tau2.task_authoring.gaia2_series.failures import CandidateValidationError

    base = make_base()
    stage = first_stage()
    draft = SeriesDraft(
        base_task_id=base.id, base_task_sha256=digest(base), stages=[stage]
    )
    invalid = "/Calendar/events/{{series_s1_create}}/title"
    if location == "reference":
        stage.checks[0].reference.pointer = invalid
    elif location == "public_result":
        stage.evidence[0].result_pointer = invalid
    elif location == "compare_to":
        stage.checks[0].compare_to = ValueRef(source="state", stage=0)
        stage.checks[0].compare_to.pointer = invalid
    elif location == "event_id":
        stage.events[0].id = "{{series_s1_create}}"
    else:
        stage.events[0].dependencies = ["{{series_s1_user}}"]
    monkeypatch.setattr(
        environment,
        "get_environment",
        lambda **kwargs: pytest.fail(
            "Invalid format must fail before creating an environment"
        ),
    )
    monkeypatch.setattr(utils, "verify_task_artifact", lambda *args: None)
    with pytest.raises(CandidateValidationError, match="must be literal"):
        verification.execute_reference(base, Path("unused"), draft)


def test_scalar_and_object_references_follow_actual_return_shape():
    from tau2.task_authoring.gaia2_series.verification import Execution, resolve_value

    execution = Execution(
        {},
        {"series_s1_create": "new-id", "series_s1_read": {"title": "Email List"}},
        [],
        [],
        {},
        {},
    )
    assert (
        resolve_value(
            ValueRef(source="result", event_id="series_s1_create", pointer=""),
            execution,
        )
        == "new-id"
    )
    assert (
        resolve_value(
            ValueRef(source="result", event_id="series_s1_read", pointer="/title"),
            execution,
        )
        == "Email List"
    )
    with pytest.raises(ValueError, match="Invalid result pointer"):
        resolve_value(
            ValueRef(source="result", event_id="series_s1_create", pointer="/title"),
            execution,
        )


def test_native_argument_templates_remain_compatible():
    from tau2.environment.scenario import resolve_references

    events = [
        ScenarioAction(id="original-id", kind="reference", tool="create"),
        ScenarioAction(
            id="read",
            kind="reference",
            tool="read",
            dependencies=["original-id"],
            arguments={"id": "{{original-id}}", "query": "object {{original-id}}"},
        ),
    ]
    validate_graph(events)
    assert resolve_references(events[1].arguments, {"original-id": "new-id"}) == {
        "id": "new-id",
        "query": "object new-id",
    }
    with pytest.raises(ValueError, match="must be literal"):
        pointer_get({"a": {}}, "/a/{{original-id}}")


@pytest.mark.parametrize("repair", [False, True])
def test_author_and_repair_receive_reference_format_prior(monkeypatch, repair):
    from types import SimpleNamespace

    from tau2.task_authoring.gaia2_series import generation

    monkeypatch.setattr(
        generation, "MaterialReader", lambda *args: SimpleNamespace(close=lambda: None)
    )
    captured = []

    def capture(config, profile, rules, payload, *args, **kwargs):
        from tau2.task_authoring.gaia2_series.schema import RepairPatch

        captured.append((rules, payload))
        if repair:
            return RepairPatch(
                base_candidate_sha256=digest(payload["failed_candidate"]),
                changes=[
                    {"path": "/events", "value": payload["failed_candidate"]["events"]}
                ],
                reason="Fixture replacement",
            )
        return first_stage()

    monkeypatch.setattr(generation, "call_model", capture)
    base = make_base()
    draft = SeriesDraft(base_task_id=base.id, base_task_sha256=digest(base))
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    execution = SimpleNamespace(results={}, checkpoints=[])
    if repair:
        generation.repair_stage(
            config,
            base,
            None,
            execution,
            {},
            draft,
            first_stage().model_dump(mode="json"),
            {"message": "bad pointer"},
            [],
        )
    else:
        from tau2.task_authoring.gaia2_series.questions import locked_requests

        token = locked_requests.set([first_stage().request, "A second fixed request"])
        try:
            generation.author_stage(config, base, None, execution, {}, draft, "", [])
        finally:
            locked_requests.reset(token)
    rules, payload = captured[0]
    assert "reference_format" in payload
    assert (
        payload["reference_format"]["scalar_create_result"]["reference"]["pointer"]
        == ""
    )
    normalized = " ".join(rules.split())
    assert "JSON pointers are concrete RFC 6901" in normalized
    assert "do not invent /title" in normalized


def complete_fixture(task, source):
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[first_stage()]
    )
    _, execution = verify_draft(task, source, draft)
    draft.stages.append(second_stage(execution.results["series_s1_create"]))
    return draft


def test_native_continuous_execution_revision_and_replay(native_source):
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    draft = complete_fixture(task, source)
    result, execution = verify_draft(task, source, draft)
    assert result["replay_verified"]
    assert len(execution.states[1]["Calendar"]["events"]) == 1
    assert len(execution.states[2]["Calendar"]["events"]) == 1
    old_id = execution.results["series_s1_create"]
    new_id = execution.results["series_s2_create"]
    assert old_id != new_id
    assert old_id not in execution.states[2]["Calendar"]["events"]
    assert execution.states[0]["Contacts"]["contacts"]["self"]["age"] == 31


def test_evidence_queries_cannot_bind_a_later_reference(native_source):
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    draft = complete_fixture(task, source)
    stage = draft.stages[1]
    stage.evidence.append(
        Evidence(
            reference=ValueRef(source="state", stage=1, pointer="/Calendar/events"),
            expected={},
            explanation="The reminder exists before it is rescheduled.",
            public_tool="Calendar__search_events",
            public_arguments={"query": "Profile review"},
            result_pointer="/0/title",
        )
    )
    stage.evidence[-1] = stage.evidence[-1].model_copy(
        update={
            "reference": ValueRef(
                source="result", stage=1, event_id="series_s1_create"
            ),
            "expected": stage.evidence[0].expected,
            "result_pointer": "/0/event_id",
        }
    )
    stage.events[1].arguments["location"] = "Stockholm"
    stage.events[2].tool = "Calendar__search_events"
    stage.events[2].arguments = {"query": "Profile review"}
    stage.checks[-1].reference.pointer = "/0/title"
    stage.checks.append(
        OutcomeCheck(
            reference=ValueRef(
                source="result",
                stage=2,
                event_id="series_s2_read",
                pointer="/0/location",
            ),
            expected="Stockholm",
            explanation="The post-write query must read the new location.",
        )
    )
    result, execution = verify_draft(task, source, draft)
    assert result["replay_verified"]
    assert execution.results["series_s2_read"][0]["location"] == "Stockholm"
    assert not any(
        (row.get("call_id") or "").startswith("evidence_") for row in execution.records
    )


def test_invalid_evidence_pointer_reports_actual_return_shape(native_source):
    from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[first_stage()]
    )
    draft.stages[0].evidence[0].result_pointer = "/self/age"
    with pytest.raises(CandidateValidationError) as caught:
        verify_draft(task, source, draft)
    details = caught.value.details
    assert details["phase"] == "evidence"
    assert details["pointer"] == "/self/age"
    assert details["path"] == "/evidence/0/result_pointer"
    assert details["code"] == "invalid_public_pointer"
    assert details["arguments"] == {"contact_id": "self"}
    assert details["evidence_index"] == 0
    assert "age" in details["result_preview"]["keys"]


@pytest.mark.parametrize(
    "failure", ["false_fact", "unreachable_fact", "wrong_answer", "private_tool"]
)
def test_reference_verifier_rejects_invalid_material_and_outcomes(
    native_source, failure
):
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    draft = complete_fixture(task, source)
    if failure == "false_fact":
        draft.stages[0].evidence[0].expected = 99
    elif failure == "unreachable_fact":
        draft.stages[0].evidence[0].result_pointer = "/email"
    elif failure == "wrong_answer":
        draft.stages[1].checks[-1].expected = "Wrong title"
    else:
        draft.stages[0].events[0].tool = "Calendar__add_calendar_event_by_attendee"
    with pytest.raises((ValueError, KeyError)):
        verify_draft(task, source, draft)


def test_read_only_material_probes_and_source_immutability(native_source):
    from tau2.task_authoring.gaia2_series.generation import MaterialReader
    from tau2.task_authoring.gaia2_series.verification import execute_reference

    task, source, _ = native_source
    before = file_digest(source / "tasks.json")
    execution = execute_reference(task, source)
    reader = MaterialReader(task, source, execution)
    try:
        with pytest.raises(ValueError, match="READ"):
            reader.read_public_tool(
                "Contacts__edit_contact", {"contact_id": "self", "updates": {"age": 99}}
            )
        assert (
            json.loads(
                reader.read_public_tool("Contacts__get_contact", {"contact_id": "self"})
            )["age"]
            == "31"
        )
    finally:
        reader.close()
    assert file_digest(source / "tasks.json") == before


def test_generation_resume_and_independent_review(native_source, monkeypatch):
    from tau2.task_authoring.gaia2_series import generation, pipeline

    task, source, dataset = native_source
    seen = []

    def author(config, base, root, execution, context, draft, feedback, calls):
        seen.append(len(draft.stages))
        return (
            first_stage()
            if not draft.stages
            else second_stage(execution.results["series_s1_create"])
        )

    monkeypatch.setattr(generation, "author_stage", author)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    report = pipeline.generate_dataset(dataset, config, [task.id])
    assert report["results"][0]["status"] == "verified"
    pipeline.generate_dataset(dataset, config, [task.id])
    assert seen == [0, 1]
    monkeypatch.setattr(
        generation,
        "review_series",
        lambda *args: ReviewDecision(
            **{
                key: True
                for key, field in ReviewDecision.model_fields.items()
                if field.annotation is bool
            }
        ),
    )
    pipeline.review_dataset(dataset, config, [task.id])
    record = read_record(dataset, task.id)
    assert not pipeline.acceptance_errors(
        record, human_required=False, implementation=pipeline.implementation_digest()
    )
    assert "missing_or_stale_human_review" in pipeline.acceptance_errors(
        record, human_required=True, implementation=pipeline.implementation_digest()
    )
    with pytest.raises(ValueError, match="800"):
        pipeline.export_dataset(dataset)
    coverage = pipeline.export_dataset(dataset, allow_partial=True)
    assert coverage["accepted"] == 1 and not coverage["complete"]
    assert not (dataset / "release").exists()
    assert (dataset / "draft-export" / "tasks-2.json").exists()
    record["draft"]["stages"][0]["request"] += " Please."
    assert "missing_or_stale_model_review" in pipeline.acceptance_errors(
        record, human_required=False, implementation=pipeline.implementation_digest()
    )


def test_provider_errors_preserve_attempt_budget(native_source, monkeypatch):
    from tau2.task_authoring.gaia2_series import generation, pipeline

    task, _, dataset = native_source

    def unavailable(*args):
        raise generation.ProviderUnavailable("network unavailable")

    monkeypatch.setattr(generation, "author_stage", unavailable)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    report = pipeline.generate_dataset(dataset, config, [task.id])
    assert report["results"][0]["status"] == "provider_error"
    assert read_record(dataset, task.id)["attempts"]["1"] == 0


def test_human_signatures_are_content_bound_and_import_is_atomic(native_source):
    from tau2.task_authoring.gaia2_series import pipeline

    task, source, dataset = native_source
    draft = complete_fixture(task, source)
    write_json(
        record_path(dataset, task.id),
        {"base_task_id": task.id, "draft": draft.model_dump(mode="json")},
    )
    decisions = dataset / "decisions.jsonl"
    value = {
        "base_task_id": task.id,
        "content_sha256": "stale",
        "reviewer": "reviewer",
        "decision": "accepted",
    }
    decisions.write_text(json.dumps(value) + "\n")
    with pytest.raises(ValueError, match="stale"):
        pipeline.import_reviews(dataset, decisions)
    assert "human" not in read_record(dataset, task.id)
    value["content_sha256"] = digest(draft)
    decisions.write_text(json.dumps(value) + "\n")
    assert pipeline.import_reviews(dataset, decisions)["imported"] == 1
    assert read_record(dataset, task.id)["human"]["reviewer"] == "reviewer"


def test_stratified_samples_are_balanced_and_disjoint():
    base = make_base()
    tasks = {}
    for category in ["search", "execution", "ambiguity", "adaptability", "time"]:
        for number in range(160):
            task = base.model_copy(deep=True)
            task.id = f"{category}_{number}"
            task.scenario.category = category
            tasks[task.id] = task
    pilot = stratified_ids(tasks, 10, 300)
    additional = stratified_ids(tasks, 15, 300, exclude=pilot)
    assert len(pilot) == 50 and len(additional) == 75
    assert not set(pilot) & set(additional)
    assert pilot == stratified_ids(tasks, 10, 300)


def test_rejected_model_review_is_repaired_without_resetting_budget(
    native_source, monkeypatch
):
    from tau2.task_authoring.gaia2_series import generation, pipeline

    task, source, dataset = native_source
    draft = complete_fixture(task, source)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    identity = pipeline._configuration(dataset, config)
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "draft": draft.model_dump(mode="json"),
            "config_identity": identity,
            "attempts": {"1": 1, "2": 1},
            "review": {
                "content_sha256": digest(draft),
                "accepted": False,
                "decision": {"reasons": ["Make the requests more precise"]},
            },
        },
    )
    feedbacks = []

    def author(config, base, root, execution, context, prefix, feedback, calls):
        feedbacks.append(feedback)
        return (
            first_stage()
            if not prefix.stages
            else second_stage(execution.results["series_s1_create"])
        )

    monkeypatch.setattr(generation, "author_stage", author)
    from tau2.task_authoring.gaia2_series.schema import RepairResult

    def repair(config, base, root, execution, context, prefix, failed, feedback, calls):
        return RepairResult(
            stage=author(
                config, base, root, execution, context, prefix, feedback, calls
            ),
            reason="Apply the independent review's requested corrections",
        )

    monkeypatch.setattr(generation, "repair_stage", repair)
    pipeline.generate_dataset(dataset, config, [task.id])
    record = read_record(dataset, task.id)
    assert record["status"] == "verified"
    assert record["attempts"] == {"1": 2, "2": 2}
    assert "Make the requests more precise" in feedbacks[0]
    assert len(record["revision_history"]) == 1
    assert "review" not in record


@pytest.mark.parametrize("review_rejected", [False, True])
def test_exhausted_recovery_skips_replay_and_preserves_rejected_draft(
    native_source, monkeypatch, review_rejected
):
    from tau2.task_authoring.gaia2_series import pipeline

    task, source, dataset = native_source
    record = {
        "base_task_id": task.id,
        "status": "needs_revision",
        "attempts": {"1": 3},
        "repair_attempts": {"1": 3},
        "recovery_attempts": {"1": 3},
        "candidate_history": [
            {
                "stage": 1,
                "valid": False,
                "candidate": first_stage().model_dump(mode="json"),
            }
        ],
    }
    if review_rejected:
        draft = complete_fixture(task, source).model_dump(mode="json")
        record.update(
            draft=draft, review={"content_sha256": digest(draft), "accepted": False}
        )
    write_json(record_path(dataset, task.id), record)
    monkeypatch.setattr(
        pipeline,
        "execute_reference",
        lambda *args: pytest.fail("Exhausted repair must not replay"),
    )
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    result = pipeline.generate_dataset(
        dataset, config, [task.id], repair=True, recovery_rounds=1
    )
    assert result["results"][0]["skipped_before_replay"]
    assert result["results"][0]["skip_reason"] == "content_attempt_budget_exhausted"
    saved = read_record(dataset, task.id)
    for key in [
        "attempts",
        "repair_attempts",
        "recovery_attempts",
        "candidate_history",
        "draft",
        "review",
    ]:
        assert saved.get(key) == record.get(key)


def test_repeated_repair_error_stops_before_larger_recovery_budget(
    native_source, monkeypatch
):
    from tau2.task_authoring.gaia2_series import generation, pipeline
    from tau2.task_authoring.gaia2_series.schema import RepairResult

    task, _, dataset = native_source
    candidate = first_stage()
    candidate.checks[0].expected = 99
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "attempts": {"1": 3},
            "candidate_history": [
                {
                    "stage": 1,
                    "valid": False,
                    "candidate": candidate.model_dump(mode="json"),
                    "error": "Old error",
                }
            ],
        },
    )
    calls = []

    def repair(*args):
        calls.append(True)
        return RepairResult(
            stage=candidate, reason="Fixture still has the same bad count"
        )

    monkeypatch.setattr(generation, "repair_stage", repair)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    for _ in range(2):
        pipeline.generate_dataset(
            dataset, config, [task.id], repair=True, recovery_rounds=5
        )
    saved = read_record(dataset, task.id)
    assert len(calls) == 3
    assert saved["recovery_stop"]["reason"] == "repeated_candidate_failure"
    assert saved["repair_attempts"]["1"] == 3
    assert saved["recovery_attempts"].get("1", 0) == 0


def test_exhausted_normal_budget_skips_environment_replay(native_source, monkeypatch):
    from tau2.task_authoring.gaia2_series import pipeline

    task, _, dataset = native_source
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "status": "needs_revision",
            "attempts": {"1": 3},
            "repair_attempts": {},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "execute_reference",
        lambda *args: pytest.fail("Exhausted task must not replay"),
    )
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    report = pipeline.generate_dataset(dataset, config, [task.id])
    assert report["results"][0]["skipped_before_replay"]
    assert read_record(dataset, task.id)["attempts"] == {"1": 3}
    assert read_record(dataset, task.id)["repair_attempts"] == {}


def test_content_retries_stop_at_the_persisted_budget(native_source, monkeypatch):
    from tau2.task_authoring.gaia2_series import generation, pipeline

    task, _, dataset = native_source

    def invalid(*args):
        raise ValueError("A required attachment is missing")

    monkeypatch.setattr(generation, "author_stage", invalid)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    pipeline.generate_dataset(dataset, config, [task.id])
    pipeline.generate_dataset(dataset, config, [task.id])
    record = read_record(dataset, task.id)
    assert record["status"] == "needs_revision"
    assert record["attempts"]["1"] == 3
    assert len(record["candidate_history"]) == 3


def test_repair_recovers_last_candidate_without_model(native_source, monkeypatch):
    from tau2.task_authoring.gaia2_series import generation, pipeline

    task, source, dataset = native_source
    complete = complete_fixture(task, source)
    prefix = complete.model_copy(deep=True)
    candidate = prefix.stages.pop()
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "draft": prefix.model_dump(mode="json"),
            "attempts": {"1": 1, "2": 3},
            "candidate_history": [
                {
                    "stage": 2,
                    "valid": False,
                    "candidate": candidate.model_dump(mode="json"),
                    "error": "A stale observation was incorrectly bound",
                }
            ],
        },
    )
    monkeypatch.setattr(
        generation, "repair_stage", lambda *args: pytest.fail("No model needed")
    )
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    pipeline.generate_dataset(dataset, config, [task.id], repair=True)
    record = read_record(dataset, task.id)
    assert record["status"] == "verified"
    assert record["draft"]["stages"][0] == prefix.stages[0].model_dump(mode="json")
    assert record["candidate_history"][-1]["recovered_without_model"]
    assert record["attempts"]["2"] == 3
    assert not record["repair_attempts"]


def test_repair_receives_failed_candidate_and_keeps_persisted_budget(
    native_source, monkeypatch
):
    from tau2.task_authoring.gaia2_series import generation, pipeline
    from tau2.task_authoring.gaia2_series.schema import RepairResult

    task, _, dataset = native_source
    candidate = first_stage()
    candidate.checks[0].expected = 99
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "attempts": {"1": 3},
            "candidate_history": [
                {
                    "stage": 1,
                    "valid": False,
                    "candidate": candidate.model_dump(mode="json"),
                    "error": "Wrong count",
                }
            ],
        },
    )
    received = []

    def repair(config, base, root, execution, context, prefix, failed, feedback, calls):
        received.append((failed, feedback))
        return RepairResult(stage=candidate, reason="Attempted repair")

    monkeypatch.setattr(generation, "repair_stage", repair)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    pipeline.generate_dataset(dataset, config, [task.id], repair=True)
    pipeline.generate_dataset(dataset, config, [task.id], repair=True)
    record = read_record(dataset, task.id)
    assert len(received) == 3
    assert received[0][0] == candidate.model_dump(mode="json")
    assert received[0][1]["phase"] == "outcome"
    assert received[0][1]["expected"]["sample"] == "99"
    assert record["attempts"]["1"] == 3
    assert record["repair_attempts"]["1"] == 3
    assert record["status"] == "needs_revision"


def test_unexpected_verification_error_does_not_rewrite_candidate(
    native_source, monkeypatch
):
    from tau2.task_authoring.gaia2_series import generation, pipeline
    from tau2.task_authoring.gaia2_series.failures import (
        VerificationInfrastructureError,
    )

    task, _, dataset = native_source
    monkeypatch.setattr(generation, "author_stage", lambda *args: first_stage())

    def broken(*args):
        raise VerificationInfrastructureError("Replay is inconsistent")

    monkeypatch.setattr(pipeline, "verify_draft", broken)
    monkeypatch.setattr(
        generation, "repair_stage", lambda *args: pytest.fail("Engine error")
    )
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    result = pipeline.generate_dataset(dataset, config, [task.id])
    assert result["results"][0]["status"] == "verification_error"
    assert read_record(dataset, task.id)["attempts"]["1"] == 0


def test_source_substitution_is_rejected(native_source):
    from tau2.task_authoring.gaia2_series.storage import source_tasks

    _, source, dataset = native_source
    (source / "tasks.json").write_text("[]")
    with pytest.raises(ValueError, match="Source changed"):
        source_tasks(dataset)


@pytest.mark.parametrize("workers", [2, 8, 16, 32])
def test_parallel_reference_environments_do_not_share_state(native_source, workers):
    from concurrent.futures import ThreadPoolExecutor

    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    draft = complete_fixture(task, source)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(verify_draft, task, source, draft) for _ in range(workers)
        ]
        executions = [future.result()[1] for future in futures]
    assert all(execution.states == executions[0].states for execution in executions)
    executions[0].states[2]["Calendar"]["events"].clear()
    assert len(executions[1].states[2]["Calendar"]["events"]) == 1


def test_call_usage_is_persisted_before_a_stage_finishes(tmp_path):
    from tau2.task_authoring.gaia2_series.pipeline import PersistentCalls

    record = {"base_task_id": "task", "status": "generating"}
    record["calls"] = PersistentCalls([], tmp_path, record)
    record["calls"].append({"model": "fake", "cost": 0.1})
    assert read_record(tmp_path, "task")["calls"] == [{"model": "fake", "cost": 0.1}]


def test_model_failure_redacts_secrets_and_restores_logging(monkeypatch):
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    monkeypatch.setattr(
        generation, "resolve_profile", lambda *args: ("fake", {}, ["test-secret-value"])
    )

    def fail(**kwargs):
        assert llm_utils.llm_log_dir.get() is None
        raise RuntimeError("Cannot authenticate test-secret-value")

    monkeypatch.setattr(llm_utils, "generate", fail)

    class Reader:
        def tools(self):
            return []

    calls = []
    before = llm_utils.llm_log_dir.get()
    with pytest.raises(generation.ProviderUnavailable, match="REDACTED"):
        generation.call_model(
            config, config.author, "rules", {}, ReviewDecision, Reader(), calls
        )
    assert "test-secret-value" not in json.dumps(calls)
    assert llm_utils.llm_log_dir.get() == before


def test_review_template_is_not_a_signature(native_source):
    from tau2.task_authoring.gaia2_series.pipeline import import_reviews, review_sheet

    task, source, dataset = native_source
    draft = complete_fixture(task, source)
    write_json(
        record_path(dataset, task.id),
        {"base_task_id": task.id, "draft": draft.model_dump(mode="json")},
    )
    review_sheet(dataset, [task.id])
    with pytest.raises(ValueError):
        import_reviews(dataset, dataset / "human-review-template.jsonl")
    assert "human" not in read_record(dataset, task.id)


def test_speech_checks_use_delivery_without_changing_native_null_return(native_source):
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    task, source, _ = native_source
    stage = first_stage()
    stage.events.append(
        ScenarioAction(
            id="series_s1_answer",
            kind="reference",
            channel="agent",
            tool="AgentUserInterface__send_message_to_user",
            arguments={"content": "The reminder is ready."},
            dependencies=["series_s1_create"],
        )
    )
    stage.checks.append(
        OutcomeCheck(
            reference=ValueRef(
                source="result",
                stage=1,
                event_id="series_s1_answer",
                pointer="/content",
            ),
            expected="The reminder is ready.",
            explanation="The reference answer was actually delivered.",
        )
    )
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[stage]
    )
    _, execution = verify_draft(task, source, draft)
    assert execution.results["series_s1_answer"] is None
    assert (
        execution.delivered_answers["series_s1_answer"]["content"]
        == "The reminder is ready."
    )
    stage.checks[-1].expected = "An undelivered claim"
    with pytest.raises(ValueError, match="Outcome failed"):
        verify_draft(task, source, draft)


def test_exact_reference_fast_path_and_wrong_state_fallback(native_source, monkeypatch):
    from tau2.domains.gaia2.environment import get_environment
    from tau2.task_authoring.gaia2_series.verification import (
        reference_goal_met,
        verify_draft,
    )

    task, source, _ = native_source
    environment = get_environment(task=task, root=source, allow_unverified=True)
    try:
        assert not reference_goal_met(environment.tools)
    finally:
        environment.close()
    draft = complete_fixture(task, source)

    def unexpected_fallback(*args):
        raise AssertionError(
            "Exact deterministic reference should not require entity backtracking"
        )

    monkeypatch.setattr("tau2.domains.gaia2.scoring.check_goal", unexpected_fallback)
    assert verify_draft(task, source, draft)[0]["valid"]


def test_retrieval_budget_forces_a_final_json_response(monkeypatch):
    from tau2.data_model.message import AssistantMessage, ToolCall
    from tau2.environment.tool import Tool
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    turns = []

    def probe() -> str:
        """Read already supplied test material."""
        return "data"

    class Reader:
        def tools(self):
            return [Tool(func=probe)]

    def generate(**kwargs):
        turns.append(kwargs["tools"])
        if kwargs["tools"]:
            return AssistantMessage(
                role="assistant",
                tool_calls=[ToolCall(id=f"t{len(turns)}", name="probe", arguments={})],
            )
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    decision = generation.call_model(
        config, config.reviewer, "rules", {}, ReviewDecision, Reader(), []
    )
    assert decision.accepted and len(turns) == 7 and turns[-1] is None


@pytest.mark.parametrize("empty", [None, "", "  "])
def test_empty_reply_does_not_poison_retry_history(monkeypatch, empty):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    class Reader:
        def tools(self):
            return []

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    requests = []

    def generate(**kwargs):
        llm_utils.validate_message_history(kwargs["messages"])
        requests.append(list(kwargs["messages"]))
        if len(requests) == 1:
            return AssistantMessage(role="assistant", content=empty)
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    calls = []
    assert generation.call_model(
        config, config.author, "rules", {}, ReviewDecision, Reader(), calls
    ).accepted
    assert len(requests) == 2
    assert calls[0]["empty_reply"] and not calls[1]["empty_reply"]
    assert not any(isinstance(m, AssistantMessage) for m in requests[1])


def test_repeated_empty_replies_are_candidate_failures(monkeypatch):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
    from tau2.utils import llm_utils

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        llm_utils.validate_message_history(kwargs["messages"])
        return AssistantMessage(role="assistant", content="")

    monkeypatch.setattr(llm_utils, "generate", generate)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    calls = []
    with pytest.raises(CandidateValidationError, match="no content"):
        generation.call_model(
            config, config.author, "rules", {}, ReviewDecision, Reader(), calls
        )
    assert len(calls) == 3
    assert all(c["empty_reply"] and not c.get("error") for c in calls)


def test_prose_is_corrected_into_json_with_tools_disabled(monkeypatch):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    turns = []

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        turns.append(kwargs)
        content = (
            "I will audit this task now."
            if len(turns) == 1
            else json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            )
        )
        return AssistantMessage(role="assistant", content=content)

    monkeypatch.setattr(llm_utils, "generate", generate)
    decision = generation.call_model(
        config, config.reviewer, "rules", {}, ReviewDecision, Reader(), []
    )
    assert decision.accepted and len(turns) == 2
    assert turns[1]["tools"] is None


def test_duplicate_goals_preserve_material_and_target_identity(tmp_path):
    from tau2.task_authoring.gaia2_series.pipeline import duplicate_groups

    tasks = {}
    for task_id in ["first", "second"]:
        base = make_base().model_copy(update={"id": task_id})
        tasks[task_id] = base
        draft = SeriesDraft(
            base_task_id=task_id,
            base_task_sha256=digest(base),
            stages=[first_stage(), second_stage("id")],
        )
        write_json(
            record_path(tmp_path, task_id),
            {"base_task_id": task_id, "draft": draft.model_dump(mode="json")},
        )
    assert duplicate_groups(tmp_path, tasks) == [["first", "second"]]
    record = read_record(tmp_path, "second")
    record["draft"]["stages"][1]["events"][0]["arguments"]["event_id"] = (
        "another-target"
    )
    write_json(record_path(tmp_path, "second"), record)
    assert duplicate_groups(tmp_path, tasks) == []


def test_code_changes_during_a_batch_invalidate_verification(tmp_path, monkeypatch):
    from tau2.task_authoring.gaia2_series import pipeline

    hashes = iter(["before", "after"])
    monkeypatch.setattr(pipeline, "implementation_digest", lambda: next(hashes))

    def worker(task_id):
        write_json(
            record_path(tmp_path, task_id),
            {"base_task_id": task_id, "verification": {"valid": True}},
        )
        return {"id": task_id, "status": "verified"}

    report = pipeline._batch(tmp_path, ["task"], worker, 1, "test")
    assert report["implementation_changed"]
    assert not read_record(tmp_path, "task")["verification"]["valid"]


def test_transient_request_retry_records_timing_and_keeps_config_identity(monkeypatch):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    identity = generation.config_identity(config)
    requests = []

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise TimeoutError("temporary timeout")
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    policy = RequestPolicy(timeout_seconds=600, attempts=2, retry_delay_seconds=0)
    token = generation.request_policy_override.set(policy)
    calls = []
    try:
        decision = generation.call_model(
            config, config.author, "rules", {}, ReviewDecision, Reader(), calls
        )
        assert generation.config_identity(config) == identity
    finally:
        generation.request_policy_override.reset(token)
    assert decision.accepted and len(requests) == 2
    assert [call["provider_attempt"] for call in calls] == [1, 2]
    assert all(
        call["timeout_seconds"] == 600 and call["elapsed_seconds"] >= 0
        for call in calls
    )
    assert calls[0]["retryable"] and calls[0]["kind"] == "provider_error"


@pytest.mark.parametrize("attempts", [1, 2])
def test_quota_error_fails_over_to_secondary_minimax(monkeypatch, attempts):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="openai/MiniMax-M3"),
        reviewer=ModelProfile(model="openai/MiniMax-M3"),
    )
    monkeypatch.setattr(
        generation,
        "fallback_profile",
        lambda *_: (
            "openai/MiniMax-M3",
            {"api_key": "secondary", "api_base": "http://secondary/v1", "timeout": 600},
        ),
    )
    requests = []

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise RuntimeError("RateLimitError: Token Plan usage limit (2056)")
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    calls = []
    token = generation.request_policy_override.set(
        RequestPolicy(attempts=attempts, timeout_seconds=600)
    )
    try:
        result = generation.call_model(
            config, config.author, "rules", {}, ReviewDecision, Reader(), calls
        )
    finally:
        generation.request_policy_override.reset(token)
    assert result.accepted
    assert requests[0].get("api_base") != requests[1].get("api_base")
    assert requests[1]["api_base"] == "http://secondary/v1"
    assert calls[0]["kind"] == "provider_error"
    assert calls[1]["api_base"] == "http://secondary/v1"
    assert calls[1]["route"] == "secondary"
    assert calls[1]["failover"] == "MINIMAX_API_KEY2/MINIMAX_BASE_URL2"


def test_quota_switch_is_shared_and_exhausted_backup_stops_calls(monkeypatch):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy
    from tau2.utils import llm_utils

    monkeypatch.setattr(generation, "_exhausted_routes", set())
    config = AuthoringConfig(
        author=ModelProfile(model="openai/MiniMax-M3"),
        reviewer=ModelProfile(model="openai/MiniMax-M3"),
    )
    monkeypatch.setattr(
        generation,
        "resolve_profile",
        lambda *_: ("openai/MiniMax-M3", {"api_base": "https://primary/v1"}, []),
    )
    monkeypatch.setattr(
        generation,
        "fallback_profile",
        lambda *_: ("openai/MiniMax-M3", {"api_base": "http://backup/v1"}),
    )
    requests = []
    backup_exhausted = False

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        requests.append(kwargs["api_base"])
        if kwargs["api_base"] == "https://primary/v1" or backup_exhausted:
            raise RuntimeError("Token Plan 用量上限 (2056)")
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    key: True
                    for key, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    token = generation.request_policy_override.set(RequestPolicy(attempts=1))
    try:
        for _ in range(2):
            assert generation.call_model(
                config, config.author, "rules", {}, ReviewDecision, Reader(), []
            ).accepted
        assert requests == [
            "https://primary/v1",
            "http://backup/v1",
            "http://backup/v1",
        ]
        backup_exhausted = True
        with pytest.raises(generation.ProviderQuotaExhausted):
            generation.call_model(
                config, config.author, "rules", {}, ReviewDecision, Reader(), []
            )
        before = len(requests)
        with pytest.raises(generation.ProviderQuotaExhausted):
            generation.call_model(
                config, config.author, "rules", {}, ReviewDecision, Reader(), []
            )
        assert len(requests) == before
    finally:
        generation.request_policy_override.reset(token)


def test_authentication_failure_is_not_retried(monkeypatch):
    from tau2.task_authoring.gaia2_series import generation
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy
    from tau2.utils import llm_utils

    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )

    class AuthenticationFailure(Exception):
        status_code = 401

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        raise AuthenticationFailure("wrong credential")

    monkeypatch.setattr(llm_utils, "generate", generate)
    token = generation.request_policy_override.set(
        RequestPolicy(attempts=2, retry_delay_seconds=0)
    )
    calls = []
    try:
        with pytest.raises(generation.ProviderUnavailable):
            generation.call_model(
                config, config.author, "rules", {}, ReviewDecision, Reader(), calls
            )
    finally:
        generation.request_policy_override.reset(token)
    assert len(calls) == 1 and not calls[0]["retryable"]


@pytest.mark.parametrize("stop_batch,expected_attempted", [(False, 2), (True, 1)])
def test_exhausted_provider_failure_is_isolated_unless_stop_requested(
    tmp_path, monkeypatch, stop_batch, expected_attempted
):
    from types import SimpleNamespace

    from tau2.task_authoring.gaia2_series import generation, pipeline
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy

    tasks = {key: make_base().model_copy(update={"id": key}) for key in ["a", "b"]}
    monkeypatch.setattr(
        pipeline,
        "source_tasks",
        lambda path: ({"source_root": str(tmp_path), "status": "authoring"}, tasks),
    )
    monkeypatch.setattr(
        pipeline, "execute_reference", lambda *args: SimpleNamespace(states={0: {}})
    )
    monkeypatch.setattr(pipeline, "context_packet", lambda *args: {})
    attempted = []

    def author(config, base, *args):
        attempted.append(base.id)
        raise generation.ProviderUnavailable("provider unavailable")

    monkeypatch.setattr(generation, "author_stage", author)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/author"),
        reviewer=ModelProfile(model="fake/reviewer"),
    )
    report = pipeline.generate_dataset(
        tmp_path,
        config,
        list(tasks),
        concurrency=1,
        request_policy=RequestPolicy(stop_batch_on_error=stop_batch),
    )
    assert len(attempted) == expected_attempted
    assert report["concurrency"] == 1
    assert read_record(tmp_path, "a")["attempts"]["1"] == 0

"""Regression for host-owned criteria, whole-block revisions and retry rounds."""

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_task_authoring.test_gaia2_series import (  # noqa: F401
    first_stage,
    make_base,
    native_source,
)

from tau2.task_authoring.gaia2_series import lightweight as light
from tau2.task_authoring.gaia2_series.compiler import compile_series
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.generation import MaterialReader
from tau2.task_authoring.gaia2_series.schema import (
    Evidence,
    ReviewDecision,
    SeriesDraft,
)
from tau2.task_authoring.gaia2_series.solutions import (
    SolutionOperation,
    SolutionRevision,
    StageSolution,
    assemble_solution,
    revise_solution,
    solution_from_stage,
)
from tau2.task_authoring.gaia2_series.storage import digest, write_json


@pytest.mark.parametrize(
    "bad_field,value",
    [
        ("request", "Changed locked request"),
        ("criteria", {"actions": [{"name": "DB"}], "reward_basis": ["DB"]}),
        ("reward_basis", ["DB"]),
    ],
)
def test_model_cannot_submit_framework_fields(bad_field, value):
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    candidate[bad_field] = value
    with pytest.raises(ValidationError):
        StageSolution.model_validate(candidate)


@pytest.mark.parametrize("reply_argument", ["content", "message"])
def test_assembly_sets_framework_fields_and_answer_channel(reply_argument):
    candidate = solution_from_stage(first_stage())
    candidate.events.append(
        SolutionOperation(
            id="series_s1_answer",
            tool="AgentUserInterface__send_message_to_user",
            arguments={reply_argument: "Reminder created"},
            dependencies=["series_s1_create"],
        )
    )
    candidate.communicate_info = ["Reminder created"]
    stage = assemble_solution(
        candidate, "This is the exact locked business request.", 1
    )
    assert stage.request == "This is the exact locked business request."
    assert stage.criteria.actions == [] and stage.criteria.env_assertions == []
    assert set(stage.criteria.reward_basis) == {
        "ENV_ASSERTION",
        "NL_ASSERTION",
        "COMMUNICATE",
    }
    assert all(event.kind == "reference" for event in stage.events)
    assert [event.channel for event in stage.events] == ["tool", "agent"]
    assert stage.events[-1].arguments == {"content": "Reminder created"}
    assert candidate.events[-1].arguments == {reply_argument: "Reminder created"}
    candidate.communicate_info = []
    assert (
        "COMMUNICATE"
        not in assemble_solution(
            candidate, first_stage().request, 1
        ).criteria.reward_basis
    )


def test_reference_reply_schema_is_available_without_a_native_application_tool():
    reader = MaterialReader.__new__(MaterialReader)
    reader.schemas = {}
    schema = json.loads(
        reader.get_tool_schema("AgentUserInterface__send_message_to_user")
    )
    assert schema["function"]["parameters"]["required"] == ["content"]


def test_event_metadata_is_not_a_model_field():
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    for field in ["kind", "channel"]:
        bad = copy.deepcopy(candidate)
        bad["events"][0][field] = "evidence"
        with pytest.raises(ValidationError):
            StageSolution.model_validate(bad)
    with pytest.raises(CandidateValidationError) as caught:
        assemble_solution(
            StageSolution.model_validate(candidate), first_stage().request, 2
        )
    assert caught.value.details["path"] == "/events/0/id"
    assert caught.value.details["stage"] == 2


def test_block_revision_fixes_0149_style_path_without_patch_addresses():
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    original = copy.deepcopy(candidate)
    evidence = copy.deepcopy(candidate["evidence"])
    evidence[0]["result_pointer"] = "/correct_shape"
    revision = SolutionRevision(
        base_candidate_sha256=digest(candidate),
        evidence=evidence,
        reason="Correct public read shape",
    )
    result = revise_solution(candidate, revision)
    assert result.evidence[0].result_pointer == "/correct_shape"
    assert candidate == original
    assert result.communicate_info == original["communicate_info"]
    assert result.nl_assertions == original["nl_assertions"]
    revision.base_candidate_sha256 = "0" * 64
    with pytest.raises(CandidateValidationError) as caught:
        revise_solution(candidate, revision)
    assert caught.value.details["code"] == "stale_candidate"
    assert caught.value.details["path"] == "/base_candidate_sha256"


@pytest.mark.parametrize(
    "payload",
    [
        {"changes": [{"path": "/evidence/0/pointer", "value": "/x"}]},
        {"request": "Other request"},
        {"nl_assertions": []},
        {"events": []},
        {},
    ],
)
def test_revision_has_no_arbitrary_paths_or_user_request_edits(payload):
    with pytest.raises(ValidationError):
        SolutionRevision.model_validate(
            {"base_candidate_sha256": "0" * 64, "reason": "Invalid fixture", **payload}
        )


@pytest.mark.parametrize("future_kind", ["checkpoint", "event"])
def test_future_evidence_feedback_identifies_exact_item(future_kind):
    base = make_base()
    part = first_stage()
    ref = part.evidence[0].reference
    if future_kind == "checkpoint":
        ref.source, ref.stage = "state", 1
        field = "stage"
    else:
        ref.source, ref.stage, ref.event_id = "result", 0, "series_s1_create"
        field = "event_id"
    with pytest.raises(CandidateValidationError) as caught:
        compile_series(
            base,
            SeriesDraft(
                base_task_id=base.id, base_task_sha256=digest(base), stages=[part]
            ),
        )
    feedback = light.short_feedback(caught.value)
    assert feedback["path"] == f"/evidence/0/reference/{field}"
    assert feedback["stage"] == 1
    assert feedback["code"].startswith("future_evidence")


def test_schema_feedback_is_short_precise_and_retains_full_disk_error():
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    for field in ["events", "evidence", "checks", "nl_assertions"]:
        candidate[field] = []
    with pytest.raises(ValidationError) as caught:
        StageSolution.model_validate(candidate)
    feedback = light.short_feedback(caught.value)
    assert len(feedback["errors"]) == 3
    assert feedback["path"] == "/events"
    assert feedback["code"] == "invalid_submission_schema"
    assert len(light.error_details(caught.value)["validation_errors"]) == 4
    assert "input_value" not in feedback["message"]
    previous = CandidateValidationError(
        "Old failure", phase="evidence", expected={"sample": "wrong person"}
    )
    assert "expected" in light.short_feedback(previous)
    assert "expected" not in light.short_feedback(caught.value)


def test_public_result_pointer_error_carries_actual_shape_and_params():
    from tau2.task_authoring.gaia2_series import verification

    evidence = Evidence(
        reference={"source": "state", "stage": 0, "pointer": "/value"},
        expected="sender@example.org",
        explanation="Read the same actual sender.",
        public_tool="Emails__list_emails",
        public_arguments={"folder_name": "SENT"},
        result_pointer="/Emails/folders/SENT/emails/94/sender",
    )
    runtime = SimpleNamespace(
        records=[
            SimpleNamespace(
                kind="tool",
                name="Emails__list_emails",
                result={"emails": [{"sender": "sender@example.org"}]},
            )
        ]
    )
    env = SimpleNamespace(
        tools=SimpleNamespace(
            _operations={"Emails__list_emails": SimpleNamespace(write_operation=False)},
            tools={"Emails__list_emails": object()},
        ),
        scenario_runtime=runtime,
        get_response=lambda *args: SimpleNamespace(error=False),
    )
    execution = verification.Execution(
        {0: {"value": "sender@example.org"}}, {}, [], [], {}, {}
    )
    with pytest.raises(CandidateValidationError) as caught:
        verification._read_evidence(env, evidence, execution, 1)
    caught.value.details["evidence_index"] = 0
    # execute_reference attaches the per-item path after a read failure.
    caught.value.details["path"] = "/evidence/0/result_pointer"
    feedback = light.short_feedback(caught.value)
    assert feedback["code"] == "invalid_public_pointer"
    assert feedback["path"] == "/evidence/0/result_pointer"
    assert feedback["result_preview"]["keys"] == ["emails"]
    assert feedback["arguments"] == {"folder_name": "SENT"}


@pytest.mark.parametrize("saved_review", [False, True])
def test_retry_round_prioritizes_failed_tasks_and_does_not_reset_on_resume(
    tmp_path, saved_review
):
    prior, new, source = tmp_path / "prior", tmp_path / "new", tmp_path / "source"
    ids = ["pending", "failed2", "failed1"]
    scope = {
        "protocol": light.PROTOCOL,
        "source_dataset": str(source.resolve()),
        "expected_tasks": 4,
        "passed_ids": ["passed"],
        "repair_ids": ids,
        "repair_order": ids,
    }
    write_json(prior / "scope.json", scope)
    write_json(new / "scope.json", scope)
    ledger = {
        "passed": {"status": "passed"},
        "pending": {"status": "pending"},
        "failed1": {"status": "blocked"},
        "failed2": {"status": "blocked"},
    }
    write_json(prior / "task-status.json", ledger)
    write_json(
        new / "task-status.json",
        {k: {"status": "passed" if k == "passed" else "pending"} for k in ledger},
    )
    saved = {}
    for key in ["failed1", "failed2"]:
        path = prior / "sessions" / f"{digest(key)}.json"
        write_json(
            path,
            {
                "task_id": key,
                "status": "blocked",
                "attempts": {"1": 3, "2": 1},
                "requests": ["Locked first question", "Locked second question"],
                "question_audit": {"verdict": "pass"},
                "verified_prefix": {"stages": [{"request": "Locked first question"}]},
            },
        )
        if key == "failed1" and saved_review:
            record = json.loads(path.read_text())
            record["verified_prefix"]["stages"].append(
                {"request": "Locked second question"}
            )
            decision = ReviewDecision(
                **{
                    k: True
                    for k, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                },
                affected_stages=[2],
                reasons=["Correct the second-stage answer from the actual records."],
            )
            decision.correct_solution = False
            record["review"] = decision.model_dump(mode="json")
            write_json(path, record)
        saved[key] = path.read_bytes()
    write_json(
        prior / "sessions" / f"{digest('pending')}.json",
        {
            "task_id": "pending",
            "status": "pending",
            "attempts": {"1": 1, "2": 0},
            "requests": ["Locked first question", "Locked second question"],
        },
    )
    light._seed_retry(new, prior.resolve())
    frozen = json.loads((new / "scope.json").read_text())
    assert frozen["repair_order"] == (
        ["failed1", "failed2", "pending"]
        if saved_review
        else ["failed2", "failed1", "pending"]
    )
    path = new / "sessions" / f"{digest('failed1')}.json"
    session = json.loads(path.read_text())
    assert session["attempts"] == {"1": 0, "2": 0}
    assert session["prior_session"]["attempts"] == {"1": 3, "2": 1}
    resumed = json.loads((new / "sessions" / f"{digest('pending')}.json").read_text())
    assert resumed["prior_session"]["attempts"] == {"1": 1, "2": 0}
    assert resumed["requests"] == ["Locked first question", "Locked second question"]
    assert session["verified_prefix"]["stages"][0]["request"] == "Locked first question"
    if saved_review:
        assert session["pending_review"]["content_sha256"] == digest(
            session["verified_prefix"]
        )
        assert session["pending_review"]["decision"]["correct_solution"] is False
    session["attempts"]["2"] = 1
    session["deadline_at"] = 42
    write_json(path, session)
    assert light.prepare_scope(source, new, retry_from=prior) == frozen
    assert json.loads(path.read_text())["attempts"]["2"] == 1
    assert json.loads(path.read_text())["deadline_at"] == 42
    assert all(
        (prior / "sessions" / f"{digest(key)}.json").read_bytes() == value
        for key, value in saved.items()
    )
    assert not (new / "batches").exists()
    assert light.publish(new)["passed"] == 1

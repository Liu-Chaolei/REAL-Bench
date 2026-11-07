"""Contracts for dynamic references, bounded submission and repair provenance."""

import copy
import json

import pytest
from test_task_authoring.test_gaia2_series import (  # noqa: F401
    first_stage,
    make_base,
    native_source,
)

from tau2.data_model.message import AssistantMessage, ToolCall
from tau2.task_authoring.gaia2_series import generation
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.repair import (
    apply_checked_patch,
    apply_patch,
    failure_feedback,
)
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    ModelProfile,
    RepairPatch,
    ValueRef,
)
from tau2.task_authoring.gaia2_series.storage import digest
from tau2.task_authoring.gaia2_series.verification import Execution, resolve_value


def test_legacy_reference_round_trip_keeps_bytes_and_digest():
    old = {
        "source": "state",
        "stage": 1,
        "event_id": None,
        "pointer": "/Calendar/events",
    }
    new = ValueRef.model_validate(old).model_dump(mode="json")
    assert new == old
    assert digest(new) == digest(old)


@pytest.mark.parametrize(
    "key,dictionary,expected",
    [
        ("created", {"created": {"members": ["a", "b"]}}, ["a", "b"]),
        (12, {}, None),
        ("missing", {}, None),
    ],
)
def test_dynamic_dictionary_key_is_typed_and_must_exist(key, dictionary, expected):
    ref = ValueRef(
        source="state",
        stage=1,
        pointer="/Chats/conversations",
        key_from_result={"event_id": "create"},
        suffix_pointer="/members",
    )
    execution = Execution(
        {1: {"Chats": {"conversations": dictionary}}}, {"create": key}, [], [], {}, {}
    )
    if expected is None:
        with pytest.raises(CandidateValidationError):
            resolve_value(ref, execution)
    else:
        assert resolve_value(ref, execution) == expected


@pytest.mark.parametrize("producer,stage", [("missing", 1), ("series_s1_create", 0)])
def test_dynamic_key_cannot_reference_unknown_or_future_event(producer, stage):
    from tau2.task_authoring.gaia2_series.compiler import compile_series
    from tau2.task_authoring.gaia2_series.schema import SeriesDraft

    base = make_base()
    part = first_stage()
    part.checks[0].reference = ValueRef(
        source="state",
        stage=stage,
        pointer="/Calendar/events",
        key_from_result={"event_id": producer},
    )
    with pytest.raises(ValueError, match="unknown or future"):
        compile_series(
            base,
            SeriesDraft(
                base_task_id=base.id, base_task_sha256=digest(base), stages=[part]
            ),
        )


def test_patch_changes_only_allowed_fields_and_preserves_original():
    original = first_stage().model_dump(mode="json")
    copy_before = copy.deepcopy(original)
    patch = RepairPatch(
        base_candidate_sha256=digest(original),
        changes=[{"path": "/checks/0/expected", "value": 2}],
        reason="Correct observed count",
    )
    result = apply_patch(original, patch)
    assert result.stage.checks[0].expected == 2
    assert result.stage.request == original["request"]
    assert original == copy_before
    patch.base_candidate_sha256 = "0" * 64
    with pytest.raises(ValueError, match="stale"):
        apply_patch(original, patch)


@pytest.mark.parametrize(
    "path",
    [
        "/request",
        "/draft",
        "/events/-1/arguments",
        "/events/80",
        "/events/0/absent",
    ],
)
def test_patch_cannot_edit_locked_fields_or_nonexistent_paths(path):
    original = first_stage().model_dump(mode="json")
    with pytest.raises((ValueError, KeyError, IndexError)):
        patch = RepairPatch(
            base_candidate_sha256=digest(original),
            changes=[{"path": path, "value": "bad"}],
            reason="Invalid fixture",
        )
        apply_patch(original, patch)


def test_patch_repairs_actions_assertions_and_consolidates_checks(native_source):  # noqa: F811
    from tau2.task_authoring.gaia2_series.schema import SeriesDraft
    from tau2.task_authoring.gaia2_series.verification import verify_draft

    base, source, _ = native_source
    original = first_stage().model_dump(mode="json")
    original["criteria"]["actions"] = [
        {
            "action_id": "calendar_add",
            "name": "Calendar__add_calendar_event",
            "arguments": original["events"][0]["arguments"],
        }
    ]
    original["criteria"]["env_assertions"] = [
        {
            "env_type": "assistant",
            "func_name": "Files__exists",
            "arguments": {"path": "/unrelated.txt"},
            "assert_value": True,
        }
    ]
    original["checks"] *= 2
    before = copy.deepcopy(original)
    patch = RepairPatch(
        base_candidate_sha256=digest(original),
        changes=[
            {"path": "/criteria/actions", "value": []},
            {"path": "/criteria/env_assertions", "value": []},
            {"path": "/checks", "value": original["checks"][:1]},
            {
                "path": "/relation",
                "value": "The requested profile reminder adds a new business goal.",
            },
            {"path": "/preconditions", "value": []},
        ],
        reason="Fix illegal framework criteria and merge redundant checks without changing the reminder request",
    )
    repaired = apply_checked_patch(original, patch)
    assert not repaired.stage.criteria.actions
    assert not repaired.stage.criteria.env_assertions
    assert len(repaired.stage.checks) == 1
    assert repaired.stage.request == original["request"]
    assert original == before
    verified, _ = verify_draft(
        base,
        source,
        SeriesDraft(
            base_task_id=base.id,
            base_task_sha256=digest(base),
            stages=[repaired.stage],
        ),
    )
    assert verified["valid"] and verified["replay_verified"]


def test_broad_criteria_patch_still_validates_stage_schema():
    original = first_stage().model_dump(mode="json")
    patch = RepairPatch(
        base_candidate_sha256=digest(original),
        changes=[{"path": "/criteria", "value": {"reward_basis": ["unknown"]}}],
        reason="Invalid reward type must still fail schema validation",
    )
    with pytest.raises(CandidateValidationError):
        apply_checked_patch(original, patch)
    patch.changes[0].path = "/checks"
    patch.changes[0].value = []
    with pytest.raises(CandidateValidationError):
        apply_checked_patch(original, patch)


def test_schema_failure_keeps_newest_patched_candidate_in_feedback():
    original = first_stage().model_dump(mode="json")
    patch = RepairPatch(
        base_candidate_sha256=digest(original),
        changes=[
            {"path": "/checks/0/reference/pointer", "value": "/objects/{{create}}"}
        ],
        reason="Invalid fixture",
    )
    with pytest.raises(CandidateValidationError) as caught:
        apply_checked_patch(original, patch)
    details, newest = failure_feedback(caught.value, original, [])
    assert newest["checks"][0]["reference"]["pointer"] == "/objects/{{create}}"
    assert details["current_candidate_sha256"] == digest(newest)
    assert digest(newest) != digest(original)


def test_stage_schema_failure_keeps_submitted_stage_not_old_candidate():
    original = first_stage().model_dump(mode="json")
    newest = {**original, "request": "The newest candidate"}
    calls = [
        {
            "purpose": "StageDraft",
            "tool_calls": [{"name": "submit_stage", "arguments": newest}],
        }
    ]
    details, current = failure_feedback(ValueError("bad schema"), original, calls)
    assert current == newest
    assert "submit_stage" in details["raw_reply"]


@pytest.fixture
def submission_env(monkeypatch):
    from tau2.environment.tool import Tool
    from tau2.utils import llm_utils

    monkeypatch.setattr(generation, "resolve_profile", lambda *args: ("fake", {}, []))
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )

    def probe() -> str:
        """Read fixture material."""
        return "data"

    class Reader:
        def tools(self):
            return [Tool(func=probe)]

    def run():
        return generation.call_model(
            config,
            config.author,
            "rules",
            {},
            ValueRef,
            Reader(),
            [],
            submission_name="submit_stage",
        )

    return llm_utils, run


def test_final_invalid_submission_gets_one_correction_without_retrieval(
    submission_env, monkeypatch
):
    llm_utils, run = submission_env
    requests = []

    def generate(**kwargs):
        llm_utils.validate_message_history(kwargs["messages"])
        requests.append([tool.name for tool in kwargs["tools"]])
        if len(requests) <= 6:
            return AssistantMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id=f"t{len(requests)}", name="probe", arguments={})
                ],
            )
        if len(requests) == 7:
            return AssistantMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id="wrong", name="submit_stage", arguments={"context": {}})
                ],
            )
        return AssistantMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="correct",
                    name="submit_stage",
                    arguments={"source": "state", "pointer": "/x"},
                )
            ],
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    assert run().pointer == "/x"
    assert len(requests) == 8
    assert requests[-2:] == [["submit_stage"], ["submit_stage"]]


@pytest.mark.parametrize("shape", ["echo", "multiple", "mixed", "empty"])
def test_invalid_submission_never_succeeds_or_retries_unbounded(
    submission_env, monkeypatch, shape
):
    llm_utils, run = submission_env
    count = 0

    def generate(**kwargs):
        nonlocal count
        count += 1
        if shape == "echo":
            return AssistantMessage(
                role="assistant", content=json.dumps({"base_task": {}, "context": {}})
            )
        if shape == "empty":
            return AssistantMessage(role="assistant", content="")
        calls = [ToolCall(id="a", name="submit_stage", arguments={"source": "state"})]
        calls.append(
            ToolCall(
                id="b",
                name="submit_stage" if shape == "multiple" else "probe",
                arguments={},
            )
        )
        return AssistantMessage(role="assistant", tool_calls=calls)

    monkeypatch.setattr(llm_utils, "generate", generate)
    with pytest.raises(CandidateValidationError):
        run()
    assert count == 2


def test_required_tool_choice_has_only_one_explicit_fallback(
    submission_env, monkeypatch
):
    llm_utils, run = submission_env
    calls = []

    class Unsupported(ValueError):
        status_code = 400

    def generate(**kwargs):
        calls.append(kwargs.get("tool_choice"))
        if len(calls) == 1:
            return AssistantMessage(role="assistant", content="wrong")
        if kwargs["tool_choice"] == "required":
            raise Unsupported("tool_choice required is not supported")
        return AssistantMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="a", name="submit_stage", arguments={"source": "state"})
            ],
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    assert run().source == "state"
    assert calls == ["auto", "required", "auto"]


def test_deepseek_thinking_uses_auto_submission_without_required_conflict(
    submission_env, monkeypatch
):
    llm_utils, run = submission_env
    calls = []
    monkeypatch.setattr(
        generation, "resolve_profile", lambda *args: ("openai/deepseek-flash", {}, [])
    )

    def generate(**kwargs):
        calls.append(kwargs["tool_choice"])
        if len(calls) <= 6:
            return AssistantMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id=f"probe-{len(calls)}", name="probe", arguments={})
                ],
            )
        return AssistantMessage(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="submit",
                    name="submit_stage",
                    arguments={"source": "state", "pointer": "/x"},
                )
            ],
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    assert run().source == "state"
    assert calls == ["auto"] * 7


def test_acceptance_session_preserves_old_budgets_and_targets_one_task(
    native_source,  # noqa: F811
    monkeypatch,
):
    from test_task_authoring.test_gaia2_series import second_stage

    from tau2.task_authoring.gaia2_series import acceptance
    from tau2.task_authoring.gaia2_series.schema import ReviewDecision
    from tau2.task_authoring.gaia2_series.storage import (
        read_record,
        record_path,
        write_json,
    )

    task, _, dataset = native_source
    saved = {
        "base_task_id": task.id,
        "status": "needs_revision",
        "attempts": {"1": 3},
        "repair_attempts": {"1": 3},
        "recovery_attempts": {"1": 3},
    }
    write_json(record_path(dataset, task.id), saved)
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/fixture"),
        reviewer=ModelProfile(model="mimo/fixture"),
    )

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
    output = dataset.parent / "acceptance"
    report = acceptance.run_acceptance(dataset, output, task.id, config)
    assert report["scope_complete"] is True and report["dataset_complete"] is False
    assert report["source_unchanged"] is True
    assert read_record(dataset, task.id) == saved
    cloned = read_record(output, task.id)
    for k in ["attempts", "repair_attempts", "recovery_attempts"]:
        assert cloned[k] == saved[k]
    assert len(list((output / "work").glob("*.json"))) == 1
    assert cloned["repair_sessions"]["acceptance-v3"]["attempts"] == {"1": 1, "2": 1}


@pytest.mark.parametrize("limit", [2, 5])
def test_session_failure_budget_survives_restart(native_source, monkeypatch, limit):  # noqa: F811
    from tau2.task_authoring.gaia2_series import pipeline
    from tau2.task_authoring.gaia2_series.storage import (
        read_record,
        record_path,
        write_json,
    )

    task, _, dataset = native_source
    write_json(
        record_path(dataset, task.id),
        {
            "base_task_id": task.id,
            "status": "needs_revision",
            "attempts": {"1": 3},
            "repair_attempts": {"1": 3},
        },
    )
    calls = []

    def fail(*args):
        calls.append(True)
        raise ValueError("Bad submission fixture")

    monkeypatch.setattr(generation, "author_stage", fail)
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    for _ in range(2):
        pipeline.generate_dataset(
            dataset,
            config,
            [task.id],
            repair=True,
            content_session="test",
            content_attempts=limit,
        )
    saved = read_record(dataset, task.id)
    expected = min(limit, 3)
    assert len(calls) == expected
    assert saved["attempts"]["1"] == 3 and saved["repair_attempts"]["1"] == 3
    assert saved["repair_sessions"]["test"]["attempts"]["1"] == expected


def test_expired_session_does_not_send_api_requests(submission_env, monkeypatch):
    from tau2.task_authoring.gaia2_series.schema import RequestPolicy

    llm_utils, run = submission_env
    token = generation.request_policy_override.set(RequestPolicy(deadline_at=1))
    monkeypatch.setattr(
        llm_utils,
        "generate",
        lambda **kwargs: pytest.fail("Expired request must not reach provider"),
    )
    try:
        with pytest.raises(generation.ProviderUnavailable, match="deadline"):
            run()
    finally:
        generation.request_policy_override.reset(token)


def test_malformed_native_arguments_are_corrected_as_submission_error(
    submission_env, monkeypatch
):
    llm_utils, run = submission_env
    requests = []

    def generate(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            raise json.JSONDecodeError("Bad native arguments", '{"source":', 10)
        return AssistantMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="fixed", name="submit_stage", arguments={"source": "state"})
            ],
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    assert run().source == "state"
    assert len(requests) == 2
    assert [t.name for t in requests[-1]["tools"]] == ["submit_stage"]


def test_failed_submission_raw_output_is_not_replaced_by_old_comment():
    original = first_stage().model_dump(mode="json")
    error = CandidateValidationError(
        "Wrong output", phase="submission", raw_reply="latest malformed submission"
    )
    feedback, _ = failure_feedback(
        error,
        original,
        [{"purpose": "RepairPatch", "content": "earlier retrieval commentary"}],
    )
    assert feedback["raw_reply"] == "latest malformed submission"

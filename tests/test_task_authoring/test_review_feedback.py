"""Review findings correct the affected solution without redoing good prefixes."""

import json

import pytest
from test_task_authoring.test_gaia2_series import first_stage, make_base, second_stage
from test_task_authoring.test_lightweight import _working
from test_task_authoring.test_lightweight import native_source as native_source

from tau2.data_model.scenario import ScenarioAction
from tau2.task_authoring.gaia2_series import (
    generation,
    verification,
)
from tau2.task_authoring.gaia2_series import (
    lightweight as light,
)
from tau2.task_authoring.gaia2_series.compiler import compile_series
from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    Evidence,
    ModelProfile,
    SeriesDraft,
)
from tau2.task_authoring.gaia2_series.solutions import (
    SolutionRevision,
    StageSolution,
    revise_solution,
    solution_from_stage,
)
from tau2.task_authoring.gaia2_series.storage import digest, write_json


def audit(accepted=True):
    result = light.LightReview(
        verdict="pass", reasons=["Business goals and answers verified."]
    )
    if not accepted:
        result.verdict = "fail"
        result.affected_stages = [2]
        result.reasons = [
            "The answer uses Antwerp's 21 minutes for Bruges; correct it from Bruges records and deliver the requested answer."
        ]
    return result


def test_compact_review_has_one_verdict_and_preserves_old_review_decisions():
    from pydantic import ValidationError

    from tau2.task_authoring.gaia2_series.schema import ReviewDecision

    compact = audit()
    assert set(compact.model_dump()) == {"verdict", "reasons", "affected_stages"}
    assert light.read_review(compact.model_dump()).accepted
    assert not light.read_review(audit(False).model_dump()).accepted
    old = ReviewDecision(
        **{
            k: True
            for k, field in ReviewDecision.model_fields.items()
            if field.annotation is bool
        }
    )
    old.no_pointless_reversal = False
    assert not light.read_review(old.model_dump()).accepted
    with pytest.raises(ValidationError):
        light.LightReview.model_validate(
            {**compact.model_dump(), "no_pointless_reversal": False}
        )


def test_answer_assertions_can_change_and_checks_can_be_consolidated():
    solution = solution_from_stage(first_stage()).model_dump(mode="json")
    solution["communicate_info"] = ["21"]
    solution["nl_assertions"] = ["Answer is 21 minutes"]
    solution["checks"] *= 2
    revised = revise_solution(
        solution,
        SolutionRevision(
            base_candidate_sha256=digest(solution),
            communicate_info=["10.5"],
            nl_assertions=["Answer is 10.5 minutes for Bruges"],
            checks=solution["checks"][:1],
            reason="Correct the reviewed business answer and merge duplicate checks",
        ),
    )
    assert revised.communicate_info == ["10.5"]
    assert revised.nl_assertions == ["Answer is 10.5 minutes for Bruges"]
    assert len(revised.checks) == 1
    assert solution["communicate_info"] == ["21"]
    cleared = revise_solution(
        solution,
        SolutionRevision(
            base_candidate_sha256=digest(solution),
            communicate_info=[],
            reason="Remove a wrong exact substring requirement; the natural-language goal remains",
        ),
    )
    assert cleared.communicate_info == []


def test_revision_can_update_linked_events_evidence_and_checks_together():
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    changed = json.loads(json.dumps(candidate))
    event_id = "series_s1_create_reminder"
    changed["events"][0]["id"] = event_id
    changed["evidence"][0].update(
        reference={"source": "result", "stage": 1, "event_id": event_id},
        expected="created-id",
        public_tool="Calendar__get_calendar_event",
        public_arguments={"event_id": "{{" + event_id + "}}"},
        result_pointer="/event_id",
    )
    changed["checks"][0].update(
        reference={"source": "result", "stage": 1, "event_id": event_id},
        expected="created-id",
        operation="equals",
    )
    revised = revise_solution(
        candidate,
        SolutionRevision(
            base_candidate_sha256=digest(candidate),
            events=changed["events"],
            evidence=changed["evidence"],
            checks=changed["checks"],
            reason="Rename the producer and update its dependent references",
        ),
    )
    assert revised.events[0].id == event_id
    assert revised.evidence[0].reference.event_id == event_id
    assert revised.checks[0].reference.event_id == event_id
    assert revised.communicate_info == candidate["communicate_info"]
    assert revised.nl_assertions == candidate["nl_assertions"]
    assert candidate == solution_from_stage(first_stage()).model_dump(mode="json")


def test_old_block_rejection_restores_business_error_in_repair_feedback():
    candidate = solution_from_stage(first_stage()).model_dump(mode="json")
    original = {
        "phase": "evidence",
        "code": "public_value_mismatch",
        "path": "/evidence/0/result_pointer",
        "actual": {"type": "str", "sample": '"Profile review"'},
        "expected": {"type": "str", "sample": '"Other title"'},
        "allowed_blocks": ["evidence"],
        "instruction": "Replace only evidence",
    }
    rejection = {
        "phase": "regression",
        "code": "unauthorized_block_change",
        "path": "/checks",
        "allowed_blocks": ["events"],
    }
    events = [
        {"candidate_sha256": digest(candidate), "feedback": original},
        {"candidate_sha256": digest(candidate), "feedback": rejection},
    ]
    feedback = light.repair_feedback(rejection, candidate, events)
    assert feedback["code"] == original["code"]
    assert feedback["path"] == original["path"]
    assert feedback["actual"] == original["actual"]
    assert feedback["expected"] == original["expected"]
    assert "allowed_blocks" not in feedback
    assert "related" in feedback["instruction"]
    assert original["allowed_blocks"] == ["evidence"]
    fallback = light.repair_feedback(rejection, candidate)
    assert fallback["code"] == "revalidate_candidate"
    assert "allowed_blocks" not in fallback


def test_review_correction_budget_persists_and_preserves_stage_one():
    base = make_base()
    prefix = SeriesDraft(
        base_task_id=base.id,
        base_task_sha256=digest(base),
        stages=[first_stage(), second_stage("fixture")],
    )
    state = {"attempts": {"1": 3, "2": 3}}
    before = prefix.stages[0].model_dump(mode="json")
    reopened = light.reopen_review(state, prefix, audit(False))
    assert (
        len(reopened.stages) == 1
        and reopened.stages[0].model_dump(mode="json") == before
    )
    assert state["latest_feedback"]["phase"] == "review"
    assert "Bruges" in state["latest_feedback"]["message"]
    assert state["attempts"] == {"1": 3, "2": 3}
    assert state["review_credits"] == {"2": 1}
    restored = json.loads(json.dumps(state))
    light.reopen_review(restored, prefix, audit(False))
    with pytest.raises(ValueError, match="bounded corrections"):
        light.reopen_review(restored, prefix, audit(False))
    assert restored["review_corrections"] == 2


@pytest.mark.parametrize("accepted_after_correction", [True, False])
def test_rejected_review_flows_back_to_solution_then_review_and_final_replay(
    native_source,  # noqa: F811
    tmp_path,
    monkeypatch,
    accepted_after_correction,
):
    output = _working(native_source, tmp_path)
    task, _, _ = native_source
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/test"), reviewer=ModelProfile(model="mimo/test")
    )
    seen, revisions, reviews, replays = [], [], [], []
    actual_verify = light.verify_draft

    def verify(*args, **kwargs):
        replays.append(kwargs.get("replay", True))
        return actual_verify(*args, **kwargs)

    monkeypatch.setattr(light, "verify_draft", verify)

    def call(config, profile, rules, packet, response_model, reader, calls, **kwargs):
        if response_model is StageSolution:
            seen.append(packet["stage_number"])
            if packet["stage_number"] == 1:
                return solution_from_stage(first_stage())
            identifier = next(
                iter(reader.reader.execution.states[1]["Calendar"]["events"])
            )
            return solution_from_stage(second_stage(identifier))
        if response_model is SolutionRevision:
            revisions.append(packet)
            assert packet["latest_error"]["phase"] == "review"
            events = packet["current_candidate"]["events"]
            if not any(e["id"] == "series_s2_answer" for e in events):
                events = events + [
                    {
                        "id": "series_s2_answer",
                        "tool": "AgentUserInterface__send_message_to_user",
                        "arguments": {"content": "Corrected answer"},
                        "dependencies": [events[-1]["id"]],
                    }
                ]
            return SolutionRevision(
                base_candidate_sha256=packet["base_candidate_sha256"],
                events=events,
                communicate_info=["Corrected answer"],
                nl_assertions=[
                    "The locked business request is fulfilled and the correct answer is delivered."
                ],
                reason="Apply independent review feedback",
            )
        if response_model is light.LightReview:
            reviews.append(packet)
            return audit(accepted_after_correction and len(reviews) > 1)
        pytest.fail("Unexpected model call")

    monkeypatch.setattr(generation, "call_model", call)
    result = light.repair_one(output, task.id, config)
    assert seen == [
        1,
        2,
    ]  # Good stage 1 is neither regenerated nor model-reviewed separately.
    assert revisions and "Bruges" in revisions[0]["latest_error"]["message"]
    assert reviews[0]["stages"][1]["actual_answers"] == []
    assert reviews[1]["stages"][1]["actual_answers"] == [
        {"content": "Corrected answer"}
    ]
    assert replays.count(True) == (1 if accepted_after_correction else 0)
    assert result["status"] == ("passed" if accepted_after_correction else "blocked"), (
        result
    )
    assert len(reviews) == (2 if accepted_after_correction else 3)
    assert result["attempts"]["1"] == 1
    before = len(reviews)
    light.repair_one(output, task.id, config)
    assert len(reviews) == before


def test_reference_answer_can_be_reviewed_without_an_extra_reply_event(
    native_source,  # noqa: F811
):
    task, source, _ = native_source
    stage = first_stage()
    stage.criteria.communicate_info = ["The reminder is ready"]
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[stage]
    )
    result, execution = verification.verify_draft(
        task, source, draft, replay=False, validation_mode="lightweight"
    )
    packet = light._review_payload(task, [stage.request], draft, execution)
    assert result["valid"]
    assert packet["stages"][0]["actual_answers"] == []
    assert packet["stages"][0]["solution"]["communicate_info"] == [
        "The reminder is ready"
    ]


def test_resumed_prefix_failure_repairs_latest_solution_without_resetting_budget(
    native_source,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    output = _working(native_source, tmp_path)
    task, _, _ = native_source
    stage = first_stage()
    stage.checks[0].expected = 2
    prefix = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[stage]
    )
    path = output / "sessions" / f"{digest(task.id)}.json"
    write_json(
        path,
        {
            "task_id": task.id,
            "attempts": {"1": 1, "2": 0},
            "requests": [stage.request, second_stage("id").request],
            "events": [],
            "verified_prefix": prefix.model_dump(mode="json"),
        },
    )
    seen = []

    def call(config, profile, rules, packet, response_model, reader, calls, **kwargs):
        seen.append(response_model)
        if response_model is SolutionRevision:
            assert packet["latest_error"]["code"] == "outcome_check_failed"
            assert "allowed_blocks" not in packet["latest_error"]
            return SolutionRevision(
                base_candidate_sha256=packet["base_candidate_sha256"],
                events=packet["current_candidate"]["events"]
                + [
                    {
                        "id": "series_s1_read",
                        "tool": "Calendar__get_calendar_event",
                        "arguments": {"event_id": "{{series_s1_create}}"},
                        "dependencies": ["series_s1_create"],
                    }
                ],
                checks=[
                    {
                        "reference": {
                            "source": "result",
                            "stage": 1,
                            "event_id": "series_s1_read",
                            "pointer": "/title",
                        },
                        "operation": "equals",
                        "expected": "Profile review",
                        "explanation": "The getter confirms the requested title",
                    }
                ],
                reason="Replace the bad check and add the read it requires",
            )
        if response_model is StageSolution:
            identifier = next(
                iter(reader.reader.execution.states[1]["Calendar"]["events"])
            )
            return solution_from_stage(second_stage(identifier))
        if response_model is light.LightReview:
            assert packet["stages"][0]["actual_answers"] == []
            return audit()
        pytest.fail("Unexpected call")

    monkeypatch.setattr(generation, "call_model", call)
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/test"), reviewer=ModelProfile(model="mimo/test")
    )
    result = light.repair_one(output, task.id, config)
    assert result["status"] == "passed", result
    assert seen[0] is SolutionRevision
    assert result["attempts"]["1"] == 2


def test_current_stage_read_is_valid_solution_evidence_but_future_stage_is_not(
    native_source,  # noqa: F811
    monkeypatch,
):
    task, source, _ = native_source
    stage = first_stage()
    getter = second_stage("id").evidence[0].public_tool
    stage.events.append(
        ScenarioAction(
            id="series_s1_read",
            kind="reference",
            tool=getter,
            arguments={"event_id": "{{series_s1_create}}"},
            dependencies=["series_s1_create"],
        )
    )
    stage.evidence = [
        Evidence(
            reference={
                "source": "result",
                "stage": 1,
                "event_id": "series_s1_read",
                "pointer": "/title",
            },
            expected="Profile review",
            explanation="Read the newly created reminder title.",
            public_tool=getter,
            public_arguments={"event_id": "{{series_s1_create}}"},
            result_pointer="/title",
        )
    ]
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[stage]
    )
    with pytest.raises(CandidateValidationError):
        compile_series(task, draft)
    # Only the final replay must re-run the native whole-episode goal check.
    checks = []
    original_goal = verification.reference_goal_met
    monkeypatch.setattr(
        verification,
        "reference_goal_met",
        lambda tools: checks.append(True) or original_goal(tools),
    )
    record, execution = verification.verify_draft(
        task, source, draft, replay=False, validation_mode="lightweight"
    )
    assert record["replay_verified"] is False and not checks
    assert execution.states[1]["Calendar"]["events"]
    final, _ = verification.verify_draft(
        task, source, draft, validation_mode="lightweight"
    )
    assert final["replay_verified"] is True and len(checks) == 1
    stage.evidence[0].reference.stage = 2
    with pytest.raises(CandidateValidationError):
        compile_series(task, draft, allow_current_evidence=True)


def test_initial_evidence_does_not_require_a_mechanical_continuity_marker(
    native_source,  # noqa: F811
):
    task, source, _ = native_source
    stage1 = first_stage()
    stage1.evidence[0].reference.source = "snapshot"
    stage1.evidence[0].reference.pointer = "/Contacts/contacts/self/first_name"
    stage1.evidence[0].expected = "Alice"
    stage1.evidence[0].result_pointer = "/first_name"
    stage2 = stage1.model_copy(deep=True)
    stage2.request = "Create a second profile review reminder at 3 PM."
    stage2.events[0].id = "series_s2_create"
    stage2.events[0].arguments.update(
        start_datetime="2024-10-16 15:00:00", end_datetime="2024-10-16 16:00:00"
    )
    stage2.checks[0].reference.stage = 2
    stage2.checks[0].expected = 2
    draft = SeriesDraft(
        base_task_id=task.id, base_task_sha256=digest(task), stages=[stage1, stage2]
    )
    final, _ = verification.verify_draft(
        task, source, draft, validation_mode="lightweight"
    )
    assert final["replay_verified"]

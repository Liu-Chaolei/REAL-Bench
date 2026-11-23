"""Authored goal changes must preserve all non-superseded source references."""

from copy import deepcopy

import pytest

from tau2.data_model.scenario import ScenarioAction, ScenarioSpec, ScenarioStage
from tau2.data_model.tasks import EvaluationCriteria, Task, UserScenario
from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.overlay import BusinessOverlay


def source():
    assertion = {
        "func_name": "assert_scenario_goal",
        "arguments": {"source_id": "source"},
        "assert_value": True,
        "env_type": "assistant",
    }
    initial = {
        "env_assertions": [assertion],
        "reward_basis": ["ENV_ASSERTION"],
        "communicate_info": ["Original answer"],
        "nl_assertions": ["Keep original constraints"],
    }
    return Task(
        id="source",
        user_scenario=UserScenario(instructions="Original request"),
        evaluation_criteria=EvaluationCriteria.model_validate(initial),
        scenario=ScenarioSpec(
            start_time=1000,
            source_id="source",
            source_sha256="0" * 64,
            snapshot="0" * 64,
            category="fixture",
            events=[
                ScenarioAction(
                    id="u0", kind="user", tool="user", arguments={"content": "First"}
                ),
                ScenarioAction(
                    id="keep",
                    kind="reference",
                    tool="write",
                    arguments={"value": "keep"},
                    dependencies=["u0"],
                ),
                ScenarioAction(
                    id="u1",
                    kind="user",
                    tool="user",
                    arguments={"content": "Last"},
                    dependencies=["keep"],
                ),
                ScenarioAction(
                    id="change",
                    kind="reference",
                    tool="write",
                    arguments={"value": "old"},
                    dependencies=["u1"],
                ),
            ],
            stages=[
                ScenarioStage(
                    stage=0, request="First", event_ids=["u0", "keep"], criteria=initial
                ),
                ScenarioStage(
                    stage=1,
                    request="Last",
                    event_ids=["u1", "change"],
                    criteria={"communicate_info": ["Old answer"]},
                ),
            ],
        ),
    )


def overlay():
    return {
        "version": "persistent-overlay-v1",
        "source_sha256": "0" * 64,
        "stage": 1,
        "superseded_event_ids": ["change"],
        "replacement_events": [],
        "criteria": {
            "reward_basis": ["ENV_ASSERTION"],
            "communicate_info": ["Cancelled"],
            "nl_assertions": ["Do not perform the cancelled write"],
        },
        "checks": [
            {
                "reference": {"source": "state", "stage": 1, "pointer": "/value"},
                "operation": "equals",
                "expected": "keep",
                "explanation": "Prior work remains",
            }
        ],
    }


def test_cancel_overlay_keeps_original_task_and_prior_effects_unchanged():
    task = source()
    before = task.model_dump(mode="json")
    derived = BusinessOverlay.model_validate(overlay()).derive(task)
    assert task.model_dump(mode="json") == before
    assert [e.id for e in derived.scenario.events] == ["u0", "keep", "u1"]
    assert derived.scenario.events[1] == task.scenario.events[1]
    assert derived.scenario.stages[0] == task.scenario.stages[0]
    assert (
        derived.evaluation_criteria.env_assertions
        == task.evaluation_criteria.env_assertions
    )
    assert derived.evaluation_criteria.communicate_info == [
        "Original answer",
        "Cancelled",
    ]


def test_modify_overlay_can_only_replace_declared_final_stage_references():
    task = source()
    data = overlay()
    data["replacement_events"] = [
        {
            "id": "change",
            "kind": "reference",
            "tool": "write",
            "arguments": {"value": "new"},
            "dependencies": ["u1"],
        }
    ]
    derived = BusinessOverlay.model_validate(data).derive(task)
    assert derived.scenario.events[-1].arguments == {"value": "new"}
    for patch in (
        {"superseded_event_ids": ["u1"]},
        {"superseded_event_ids": ["keep"]},
        {"source_sha256": "1" * 64},
        {
            "replacement_events": [
                {"id": "injected", "kind": "environment", "tool": "write"}
            ]
        },
    ):
        with pytest.raises(ValueError):
            BusinessOverlay.model_validate({**overlay(), **patch}).derive(task)


def test_mutations_require_overlay_and_explicit_combination_basis():
    profile = {
        "scenario_id": "cancel",
        "bindings": [{"source_task_id": "source", "source_stage": 1}],
        "requests": [
            {
                "request_id": "r1",
                "text": "Cancel it",
                "state": "running",
                "operation": "cancel",
                "reply_expectation": "Confirm actual cancellation",
            }
        ],
    }
    draft_profile = PersistentTaskConfig.model_validate(profile)
    assert draft_profile.status == "draft"
    with pytest.raises(ValueError, match="require an explicit"):
        PersistentTaskConfig.model_validate({**profile, "status": "verified"})
    parsed = PersistentTaskConfig.model_validate({**profile, "overlay": overlay()})
    parsed.validate_task(source())
    with pytest.raises(ValueError, match="reward basis"):
        PersistentTaskConfig.model_validate(
            {**profile, "overlay": overlay(), "combine_reward": True}
        ).validate_task(source())
    enabled = deepcopy(overlay())
    enabled["criteria"]["reward_basis"].append("PERSISTENT_TASK")
    PersistentTaskConfig.model_validate(
        {**profile, "overlay": enabled, "combine_reward": True}
    ).validate_task(source())


def test_cancellation_preserves_reference_effects_completed_before_receipt():
    task = source()
    derived = BusinessOverlay.model_validate(overlay()).derive(
        task, completed_before_cancel={"change"}
    )
    assert [event.id for event in derived.scenario.events] == [
        event.id for event in task.scenario.events
    ]
    assert derived.scenario.events[-1].arguments == {"value": "old"}


def test_combined_overlay_reward_does_not_multiply_business_reward_twice(monkeypatch):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import RewardInfo, SimulationRun, TerminationReason
    from tau2.data_model.tasks import RewardType
    from tau2.orchestrator.modes import CommunicationMode
    from tau2.persistent_tasks.overlay import evaluate_overlay

    authored = overlay()
    authored["criteria"]["reward_basis"].append("PERSISTENT_TASK")
    config = PersistentTaskConfig.model_validate(
        {
            "scenario_id": "cancel",
            "combine_reward": True,
            "bindings": [{"source_task_id": "source", "source_stage": 1}],
            "requests": [
                {
                    "request_id": "r1",
                    "text": "Cancel",
                    "operation": "cancel",
                    "state": "running",
                    "reply_expectation": "Confirm",
                }
            ],
            "overlay": authored,
        }
    )
    summary = {"config": config.model_dump(mode="json"), "operations": []}
    simulation = SimulationRun(
        id="test",
        task_id="source",
        start_time="2026-01-01T00:00:00",
        end_time="2026-01-01T00:00:01",
        duration=1,
        termination_reason=TerminationReason.USER_STOP,
        info={"persistent_tasks": summary},
    )
    replies = iter(
        [
            RewardInfo(reward=0.2, reward_basis=[RewardType.ENV_ASSERTION]),
            RewardInfo(reward=0.6, reward_basis=[RewardType.ENV_ASSERTION]),
        ]
    )
    monkeypatch.setattr(
        "tau2.evaluator.evaluator.evaluate_simulation", lambda *a, **kw: next(replies)
    )
    monkeypatch.setattr(
        "tau2.evaluator.persistent_tasks.grader.grade",
        lambda *a, **kw: {"reward": 0.5},
    )
    reward = evaluate_overlay(
        simulation,
        source(),
        evaluation_type="all",
        solo_mode=False,
        domain="gaia2",
        mode=CommunicationMode.FULL_DUPLEX,
        env_kwargs={},
        strict_replay=True,
        communicate_scoring=CommunicateScoring.model_validate({}),
        customer_language=None,
    )
    assert reward.reward == 0.3
    assert simulation.info["persistent_tasks"]["grading"]["completion_score"] == 0.5
    assert (
        "business_overlay_reward" not in simulation.info["persistent_tasks"]["grading"]
    )
    assert reward.info["original_gaia2_reward"]["reward"] == 0.2
    assert reward.info["derived_gaia2_reward"]["reward"] == 0.6
    assert reward.reward_breakdown[RewardType.PERSISTENT_TASK] == 0.5


def test_released_mutation_is_the_callers_current_goal_without_disclosing_checks():
    from types import SimpleNamespace

    from tau2.user.user_simulator import UserSimulator

    caller = SimpleNamespace(
        scenario_stage_ids={"original"}, scenario_goal_update_ids={"cancel"}
    )
    reminder = UserSimulator.scenario_stage_context(
        caller, "cancel", "Cancel that task and leave completed work in place."
    )
    assert "CURRENT request" in reminder.content
    assert "Cancel that task" in reminder.content
    assert "superseded_event_ids" not in reminder.content
    assert (
        UserSimulator.scenario_stage_context(caller, "progress", "How is it going?")
        is None
    )

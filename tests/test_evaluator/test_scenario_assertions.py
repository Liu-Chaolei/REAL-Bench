"""Shared natural-language checks receive timing evidence and fail closed."""

import pytest

from tau2.data_model.message import AssistantMessage
from tau2.data_model.scenario import ScenarioSpec
from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator


def test_time_assertion_receives_actual_execution_times(monkeypatch):
    expectation = (
        "The booking must not start before the three-minute waiting period ends."
    )
    task = Task(
        id="time",
        user_scenario=UserScenario(instructions=expectation),
        evaluation_criteria=EvaluationCriteria(
            nl_assertions=[expectation], reward_basis=[RewardType.NL_ASSERTION]
        ),
    )

    def judge(**kwargs):
        prompt = kwargs["messages"][-1].content
        assert '"started_time": 1010' in prompt
        assert '"name": "book"' in prompt
        return AssistantMessage(
            role="assistant",
            content=__import__("json").dumps(
                {
                    "results": [
                        {
                            "expectedOutcome": expectation,
                            "metExpectation": False,
                            "reasoning": "The booking occurred after ten seconds, before three minutes.",
                        }
                    ]
                }
            ),
        )

    monkeypatch.setattr("tau2.evaluator.evaluator_nl_assertions.generate", judge)
    reward = NLAssertionsEvaluator.calculate_reward(
        task,
        [],
        environment_trace=[
            {
                "time": 1011,
                "started_time": 1010,
                "kind": "tool",
                "name": "book",
                "arguments": {},
                "error": False,
            }
        ],
    )
    assert reward.reward == 0


def test_empty_judge_results_cannot_pass_declared_assertions(monkeypatch):
    monkeypatch.setattr(
        "tau2.evaluator.evaluator_nl_assertions.generate",
        lambda **kwargs: AssistantMessage(role="assistant", content='{"results":[]}'),
    )
    with pytest.raises(ValueError, match="exactly one result"):
        NLAssertionsEvaluator.evaluate_nl_assertions(
            [], ["The requested condition holds"]
        )


def test_voice_nl_prompt_uses_first_version_asr_tolerance(monkeypatch):
    seen = {}

    def judge(**kwargs):
        seen["system"] = kwargs["messages"][0].content
        return AssistantMessage(
            role="assistant",
            content='{"results":[{"assertion_id":"a0","metExpectation":true,"reasoning":"same words"}]}',
        )

    monkeypatch.setattr("tau2.evaluator.evaluator_nl_assertions.generate", judge)
    task = Task(
        id="0017_z5g3da3kgwcmuxllf1sxvz6esfrbs87w",
        user_scenario=UserScenario(instructions="Find the contact."),
        scenario=ScenarioSpec(
            start_time=0,
            source_id="0017_z5g3da3kgwcmuxllf1sxvz6esfrbs87w",
            source_sha256="0" * 64,
            snapshot="1" * 64,
            category="search",
            events=[],
        ),
        evaluation_criteria=EvaluationCriteria(
            nl_assertions=["The reply confirms the exact subject."],
            reward_basis=[RewardType.NL_ASSERTION],
        ),
    )
    assert NLAssertionsEvaluator.calculate_reward(task, []).reward == 1
    assert "VOICE/ASR TRANSCRIPT HANDLING" in seen["system"]
    assert "same words" in seen["system"]
    assert "already persisted in the environment" in seen["system"]
    assert "Persistence alone does not make" not in seen["system"]


def test_required_runtime_fact_uses_actual_result_and_missing_fact_fails():
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

    task = Task(
        id="confirmation",
        user_scenario=UserScenario(instructions="Create a booking"),
        evaluation_criteria=EvaluationCriteria(
            communicate_info=["Your confirmation is {{created}}"],
            reward_basis=[RewardType.COMMUNICATE],
        ),
    )
    messages = [
        AssistantMessage(role="assistant", content="Your confirmation is actual-123")
    ]
    scoring = CommunicateScoring(scorer="substring")
    assert (
        CommunicateEvaluator.calculate_reward(
            task, messages, scoring=scoring, runtime_bindings={"created": "actual-123"}
        ).reward
        == 1
    )
    missing = CommunicateEvaluator.calculate_reward(
        task, messages, scoring=scoring, runtime_bindings={}
    )
    assert missing.reward == 0
    assert "verified runtime result" in missing.communicate_checks[0].justification


def test_saved_scenario_regrading_retains_task_scoring_and_strict_replay(monkeypatch):
    from tau2.data_model.scenario import ScenarioSpec
    from tau2.data_model.simulation import (
        Info,
        Results,
        RewardInfo,
        SimulationRun,
        TerminationReason,
        UserInfo,
    )
    from tau2.environment.environment import EnvironmentInfo
    from tau2.scripts import evaluate_trajectories as grading

    task = Task(
        id="selected-not-default",
        user_scenario=UserScenario(instructions="Perform the selected task"),
        scenario=ScenarioSpec(
            start_time=1000,
            source_id="selected-not-default",
            source_sha256="0" * 64,
            snapshot="1" * 64,
            category="execution",
            events=[],
        ),
    )
    results = Results(
        info=Info(
            git_commit="fixture",
            num_trials=1,
            max_steps=100,
            max_errors=10,
            user_info=UserInfo(implementation="user_simulator"),
            agent_info={"implementation": "llm_agent"},
            environment_info=EnvironmentInfo(domain_name="gaia2", policy="test"),
        ),
        tasks=[task],
        simulations=[
            SimulationRun(
                id="saved",
                task_id=task.id,
                start_time="start",
                end_time="end",
                duration=1,
                messages=[],
                environment_trace=[],
                termination_reason=TerminationReason.USER_STOP,
            )
        ],
    )
    seen = []

    def evaluate(**kwargs):
        assert kwargs["strict_replay"] is True
        assert kwargs["env_kwargs"]["task"] == task
        assert kwargs["env_kwargs"]["semantic_scoring"] == kwargs["communicate_scoring"]
        assert kwargs["simulation"].environment_trace == []
        seen.append(kwargs["task"].id)
        return RewardInfo(reward=1)

    monkeypatch.setattr(grading, "evaluate_simulation", evaluate)
    rescored = grading.compute_simulation_rewards(results)
    assert rescored.simulations[0].reward_info.reward == 1
    assert seen == [task.id]

"""Regressions for grading failures observed in the Gaia2 voice sample."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.simulation import SimulationRun, TerminationReason
from tau2.domains.gaia2.scoring import _equivalent, business_state
from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator


def judge(monkeypatch, results):
    monkeypatch.setattr(
        "tau2.evaluator.evaluator_nl_assertions.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content=json.dumps({"results": results})
        ),
    )


def test_0304_trailing_space_does_not_reject_a_passing_judge(monkeypatch):
    judge(
        monkeypatch,
        [
            {
                "expectedOutcome": "Return None",
                "metExpectation": True,
                "reasoning": "Zero threads",
            }
        ],
    )
    checks = NLAssertionsEvaluator.evaluate_nl_assertions([], ["Return None "])
    assert checks[0].met
    assert checks[0].nl_assertion == "Return None "


def test_assertion_ids_allow_reordering_without_echoing_text(monkeypatch):
    judge(
        monkeypatch,
        [
            {
                "assertion_id": "a1",
                "metExpectation": False,
                "reasoning": "second failed",
            },
            {"assertion_id": "a0", "metExpectation": True, "reasoning": "first passed"},
        ],
    )
    checks = NLAssertionsEvaluator.evaluate_nl_assertions([], ["first", "second"])
    assert [(c.nl_assertion, c.met) for c in checks] == [
        ("first", True),
        ("second", False),
    ]


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"assertion_id": "a0", "metExpectation": "true", "reasoning": "bad type"}],
        [{"assertion_id": "a9", "metExpectation": True, "reasoning": "unknown ID"}],
    ],
)
def test_malformed_judge_results_still_fail(monkeypatch, results):
    judge(monkeypatch, results)
    with pytest.raises(ValueError):
        NLAssertionsEvaluator.evaluate_nl_assertions([], ["first"])


def test_0353_fixed_calendar_ids_are_not_permuted():
    ids = [f"{i:032x}" for i in range(358)]
    original = {k: {"event_id": k, "tag": None} for k in ids}
    actual = {**original, "a" * 32: {"event_id": "a" * 32, "tag": "Work"}}
    expected = {**deepcopy(original), "b" * 32: {"event_id": "b" * 32, "tag": None}}
    assert not _equivalent(
        business_state(actual, set(ids)), business_state(expected, set(ids))
    )
    actual["a" * 32]["tag"] = None
    assert _equivalent(
        business_state(actual, set(ids)), business_state(expected, set(ids))
    )
    actual[ids[0]]["tag"] = "unexpected mutation"
    assert not _equivalent(
        business_state(actual, set(ids)), business_state(expected, set(ids))
    )


def test_new_identity_mapping_preserves_references_and_creation_order():
    def state(ids, reversed_order=False, wrong=False):
        entries = [(ids[0], {"title": "A"}), (ids[1], {"title": "B"})]
        if reversed_order:
            entries.reverse()
        return business_state(
            {"objects": dict(entries), "reference": ids[1] if wrong else ids[0]}, set()
        )

    actual = state(["a" * 32, "b" * 32])
    assert _equivalent(actual, state(["c" * 32, "d" * 32], True))
    assert not _equivalent(actual, state(["c" * 32, "d" * 32], True, True))


@pytest.mark.parametrize("mode", ["half_duplex", "full_duplex"])
def test_scoring_failure_preserves_the_completed_trajectory(monkeypatch, mode):
    from tau2.data_model.tasks import Task, UserScenario
    from tau2.runner.simulation import run_simulation

    task = Task(id="fixture", user_scenario=UserScenario(instructions="Help"))
    sim = SimulationRun(
        id="original",
        task_id=task.id,
        start_time="start",
        end_time="end",
        duration=42,
        termination_reason=TerminationReason.USER_STOP,
        mode=mode,
        messages=[UserMessage(role="user", content="Done")],
        environment_trace=[],
    )
    orch = SimpleNamespace(
        call_context=None,
        task=task,
        run=lambda: sim,
        agent=SimpleNamespace(),
        user=SimpleNamespace(),
        environment=SimpleNamespace(
            get_policy=lambda: "policy", get_domain_name=lambda: "mock"
        ),
    )
    saved = []

    def fail(**kw):
        assert saved[0].evaluation_status == "pending"
        raise ValueError("Judge failed")

    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", fail)
    result = run_simulation(
        orch, on_trajectory_ready=lambda s: saved.append(s.model_copy(deep=True))
    )
    assert result.id == "original" and result.duration == 42
    assert result.termination_reason == TerminationReason.USER_STOP
    assert result.messages[0].content == "Done"
    assert result.evaluation_status == "failed" and result.reward_info is None
    assert result.evaluation_error["message"] == "Judge failed"


def test_grading_failures_roundtrip_and_are_excluded_from_metrics(
    tmp_path, monkeypatch
):
    from tau2.data_model.simulation import Info, Results, RewardInfo, UserInfo
    from tau2.data_model.tasks import EvaluationCriteria, Task, UserScenario
    from tau2.environment.environment import EnvironmentInfo
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.scripts import evaluate_trajectories as grading

    task = Task(
        id="fixture",
        user_scenario=UserScenario(instructions="Help"),
        evaluation_criteria=EvaluationCriteria(actions=[]),
    )
    runs = [
        SimulationRun(
            id=str(i),
            task_id=task.id,
            trial=i,
            start_time="start",
            end_time="end",
            duration=42,
            termination_reason=TerminationReason.USER_STOP,
            messages=[UserMessage(role="user", content="Done")],
            evaluation_status="failed" if i else "completed",
            reward_info=None if i else RewardInfo(reward=1),
            evaluation_error={"message": "failed"} if i else None,
        )
        for i in range(2)
    ]
    results = Results(
        info=Info(
            git_commit="test",
            num_trials=2,
            max_steps=100,
            max_errors=10,
            user_info=UserInfo(implementation="user_simulator"),
            agent_info={"implementation": "llm_agent"},
            environment_info=EnvironmentInfo(domain_name="mock", policy="policy"),
        ),
        tasks=[task],
        simulations=runs,
    )
    results.save(tmp_path / "saved", format="dir")
    restored = Results.load(tmp_path / "saved")
    assert restored.simulation_index[1].evaluation_status == "failed"
    assert restored.simulations[1].duration == 42 and restored.simulations[1].messages
    metrics = compute_metrics(restored)
    assert metrics.avg_reward == 1 and metrics.total_simulations == 1
    assert metrics.grading_error_count == 1
    monkeypatch.setattr(
        grading, "evaluate_simulation", lambda **kw: RewardInfo(reward=1)
    )
    rescored = grading.compute_simulation_rewards(restored)
    assert rescored.simulations[1].evaluation_status == "completed"
    assert rescored.simulations[1].evaluation_error is None
    assert restored.simulations[1].evaluation_status == "failed"
    restored.simulations = restored.simulations[1:]
    assert compute_metrics(restored).total_simulations == 0


def test_batch_writes_raw_trajectory_before_a_judge_failure(tmp_path, monkeypatch):
    from tau2.data_model.simulation import TextRunConfig
    from tau2.registry import registry
    from tau2.runner.batch import run_single_task
    from tau2.user.user_simulator import UserSimulator

    task = registry.get_tasks_loader("mock")()[0]
    config = TextRunConfig(domain="mock", llm_agent="unused", llm_user="unused")
    monkeypatch.setattr(
        UserSimulator,
        "generate_next_message",
        lambda self, message, state: (
            UserMessage(role="user", content="###STOP###"),
            state,
        ),
    )
    monkeypatch.setattr(
        "tau2.utils.llm_utils._generate_once",
        lambda *a, **kw: pytest.fail("This regression must not call a model"),
    )

    def fail(**kwargs):
        raw = json.loads(next(tmp_path.rglob("trajectory.json")).read_text())
        assert raw["evaluation_status"] == "pending" and raw["messages"]
        assert raw["trial"] == 0
        raise ValueError("Fixture judge failure")

    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", fail)
    sim = run_single_task(config, task, trial=0, save_dir=tmp_path)
    raw = SimulationRun.model_validate_json(
        next(tmp_path.rglob("trajectory.json")).read_text()
    )
    assert raw.id == sim.id and raw.duration == sim.duration
    assert sim.evaluation_status == "failed" and sim.messages
    assert sim.termination_reason == TerminationReason.USER_STOP

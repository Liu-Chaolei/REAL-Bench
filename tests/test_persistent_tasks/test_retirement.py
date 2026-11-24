"""Retired task protocols cannot run; saved results remain readable."""

import argparse
from types import SimpleNamespace

import pytest

from tau2.data_model.scenario import ScenarioSpec
from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.simulation import Info, Results, SimulationRun, TextRunConfig
from tau2.data_model.tasks import Task, UserScenario
from tau2.environment.environment import EnvironmentInfo
from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
from tau2.persistent_tasks.availability import reject_retired_task
from tau2.persistent_tasks.scores import completion_score
from tau2.runner.checkpoint import try_resume


def task(embedded=None):
    return Task(
        id="old",
        user_scenario=UserScenario(instructions="A saved request"),
        scenario=ScenarioSpec(
            start_time=0,
            source_id="source",
            source_sha256="0" * 64,
            snapshot="0" * 64,
            category="test",
            events=[],
            async_interaction=embedded,
        ),
    )


def test_cli_rejects_removed_option(capsys):
    from tau2.cli import add_run_args

    parser = argparse.ArgumentParser()
    add_run_args(parser)
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--persistent-task-config", "old.json"])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("config", [{}, {"scenario_id": "old"}])
def test_retired_config_is_rejected_instead_of_ignored(config):
    with pytest.raises(ValueError, match="persistent_task_config is retired"):
        TextRunConfig(domain="gaia2", persistent_task_config=config)


def test_current_profiles_do_not_gain_retired_settings():
    config = TextRunConfig(domain="gaia2", active_metrics={})
    assert "persistent_task_config" not in config.model_dump()


def test_embedded_protocol_rejection_preserves_variant_and_plain_tasks():
    reject_retired_task(task())
    reject_retired_task(task({"version": "gaia2-interaction-variant-v1"}))
    with pytest.raises(ValueError, match="scenarios are retired"):
        reject_retired_task(task({"version": "qwen-persistent-v1"}))


@pytest.mark.parametrize("total", [0.0, None])
def test_saved_scores_remain_readable_without_replay_or_regrading(tmp_path, total):
    simulation = SimulationRun(
        id="saved",
        task_id="old",
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason="user_stop",
        info={"persistent_tasks": {"grading": {"completion_score": total}}},
    )
    info = Info(
        git_commit="old",
        num_trials=1,
        max_steps=10,
        max_errors=1,
        user_info={"implementation": "user_simulator"},
        agent_info={"implementation": "llm_agent"},
        environment_info=EnvironmentInfo(domain_name="mock", policy="test"),
        persistent_task_config={"scenario_id": "old"},
    )
    saved = Results(info=info, tasks=[task()], simulations=[simulation])
    path = tmp_path / "results.json"
    saved.save(path)
    loaded = Results.load(path)
    assert (
        completion_score(loaded.simulations[0].info["persistent_tasks"]["grading"])
        == total
    )
    assert loaded.info.persistent_task_config == {"scenario_id": "old"}
    original = path.read_bytes()
    with pytest.raises(ValueError, match="regrading is retired"):
        evaluate_simulation(
            loaded.simulations[0],
            loaded.tasks[0],
            EvaluationType.ALL,
            False,
            "mock",
            communicate_scoring=CommunicateScoring(scorer="substring"),
        )
    with pytest.raises(ValueError, match="checkpoint resume is retired"):
        try_resume(path, saved, saved.tasks, 1, auto_resume=True)
    assert path.read_bytes() == original


def test_new_runner_rejects_old_task_before_provider_work():
    from tau2.runner.simulation import run_simulation

    # No Agent or Environment is needed: rejection precedes their construction/use.
    orchestrator = SimpleNamespace(task=task({"version": "qwen-persistent-v1"}))
    with pytest.raises(ValueError, match="scenarios are retired"):
        run_simulation(orchestrator)

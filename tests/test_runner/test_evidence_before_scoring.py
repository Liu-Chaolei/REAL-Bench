"""Persistence precedes every grader; individual grader failures preserve facts."""

import json
from types import SimpleNamespace

import pytest

from tau2.data_model.diagnostics import DiagnosticEvidence
from tau2.data_model.simulation import RewardInfo, SimulationRun
from tau2.data_model.tasks import Task, UserScenario
from tau2.runner.simulation import run_simulation


def prepared():
    simulation = SimulationRun(
        id="s",
        task_id="t",
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason="user_stop",
        info={
            "active_metrics": {"metrics": {"persistent_task": {"status": "completed"}}},
            "scenario_mechanisms": {
                "mechanisms": {"interaction_variant": {"status": "completed"}}
            },
        },
    )
    orchestrator = SimpleNamespace(
        call_context=None,
        task=Task(id="t", user_scenario=UserScenario(instructions="work")),
        run=lambda: simulation,
        agent=SimpleNamespace(),
        user=SimpleNamespace(),
        environment=SimpleNamespace(
            get_policy=lambda: "policy", get_domain_name=lambda: "gaia2"
        ),
    )
    return orchestrator


@pytest.mark.parametrize(
    "failure", [None, "main", "active", "variant", "collection", "diagnostics"]
)
def test_saved_trajectory_and_evidence_precede_independent_graders(
    tmp_path, monkeypatch, failure
):
    events = []
    raw = tmp_path / "trajectory.json"

    class Recorder:
        def __init__(self, *args):
            pass

        def close(self):
            pass

    def save(simulation):
        events.append("save")
        assert simulation.trial == 2
        assert simulation.evaluation_status == "pending"
        assert "scores" not in simulation.info["active_metrics"]
        assert "scores" not in simulation.info["scenario_mechanisms"]
        raw.write_text(simulation.model_dump_json())

    def collect(orchestrator, simulation, recorder, directory):
        assert raw.exists()
        events.append("collection")
        if failure == "collection":
            raise ValueError("missing media")
        directory.mkdir(parents=True)
        evidence = DiagnosticEvidence(simulation_id="s", task_id="t", domain="gaia2")
        (directory / "evidence.json").write_text(evidence.model_dump_json())
        return evidence

    def grade(name):
        def evaluate(*args, **kwargs):
            assert raw.exists()
            if failure != "collection":
                assert (tmp_path / "diagnostics/evidence/evidence.json").exists()
            events.append(name)
            if failure == name:
                raise RuntimeError(name)
            return (
                RewardInfo(reward=1)
                if name == "main"
                else {"status": "measured", "completion_score": 1}
            )

        return evaluate

    def diagnostics(*args, **kwargs):
        events.append("diagnostics")
        assert raw.exists()
        if failure == "diagnostics":
            raise ValueError("judge failure")
        assert kwargs["precomputed"]["persistent_task"] is not None
        return {}

    monkeypatch.setattr("tau2.diagnostics.collect.EvidenceRecorder", Recorder)
    monkeypatch.setattr("tau2.diagnostics.collect.finalize_evidence", collect)
    monkeypatch.setattr("tau2.diagnostics.suite.evaluate_evidence", diagnostics)
    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", grade("main"))
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.evaluator.score_persistent_task",
        grade("active"),
    )
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.variant_evaluator.score_interaction_variant",
        grade("variant"),
    )
    result = run_simulation(
        prepared(),
        on_trajectory_ready=save,
        trial=2,
        diagnostics_dir=tmp_path / "diagnostics",
        metrics_config={"measures": [], "auto_score": True},
    )
    assert events[:5] == ["save", "collection", "main", "active", "variant"]
    assert ("diagnostics" in events) == (failure != "collection")
    saved = json.loads(raw.read_text())
    assert saved["evaluation_status"] == "pending"
    assert "scores" not in saved["info"]["active_metrics"]
    assert result.termination_reason.value == "user_stop"
    assert result.evaluation_status == ("failed" if failure == "main" else "completed")
    for name, field, metric in [
        ("active", "active_metrics", "persistent_task"),
        ("variant", "scenario_mechanisms", "interaction_variant"),
    ]:
        score = result.info[field]["scores"][metric]
        assert score["status"] == ("grader_error" if failure == name else "measured")
        assert score["completion_score"] == (None if failure == name else 1)
    if failure == "collection":
        assert result.diagnostics_ref["status"] == "collection_error"


def test_save_failure_never_starts_a_grader(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A grader cannot precede successful persistence")

    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", forbidden)
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.evaluator.score_persistent_task", forbidden
    )
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.variant_evaluator.score_interaction_variant",
        forbidden,
    )

    def save(_):
        raise OSError("disk failure")

    with pytest.raises(OSError, match="disk failure"):
        run_simulation(prepared(), on_trajectory_ready=save)

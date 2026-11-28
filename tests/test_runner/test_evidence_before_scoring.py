"""Persistence precedes every grader; individual grader failures preserve facts."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event
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


@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize("complete", [False, True])
def test_state_drain_overlaps_outcomes_and_independent_metrics(
    monkeypatch, tmp_path, concurrency, complete
):
    """Final evidence is fixed; only state waits, and persistent is graded once."""
    from tau2.diagnostics.intent.io import write_json
    from tau2.diagnostics.state.capture import CAPTURE_VERSION

    sealed, release, independent, outcomes = (Event() for _ in range(4))
    context = ContextVar("test_grading_context")
    calls = []

    class Recorder:
        def __init__(self, *args):
            pass

        def set_state_event_sink(self, sink):
            self.sink = sink

    class Worker:
        def __init__(self, task, recorder, config, directory, **kwargs):
            self.directory = directory

        def start(self):
            pass

        def submit(self, event):
            pass

        def close_and_drain(self, timeout):
            assert sealed.is_set()
            assert context.get() == "preserved"
            assert release.wait(3)
            if complete:
                write_json(
                    self.directory / "status.json",
                    {"status": "complete", "capture_version": CAPTURE_VERSION},
                )
                write_json(self.directory / "state_trace.json", {})
                write_json(
                    self.directory / "state_interaction.json", {"status": "measured"}
                )

    evidence = DiagnosticEvidence(simulation_id="s", task_id="t", domain="gaia2")
    before = evidence.model_dump(mode="json")

    def collect(*args):
        sealed.set()
        return evidence

    def formal(**kwargs):
        assert sealed.is_set() and not release.is_set()
        assert context.get() == "preserved"
        return RewardInfo(reward=1)

    def persistent(*args):
        calls.append("persistent")
        outcomes.set()
        return {"status": "measured", "completion_score": 1}

    def voice(received, directory):
        assert sealed.is_set() and not release.is_set()
        assert received.model_dump(mode="json") == before
        independent.set()
        return {"status": "measured"}

    def offline_state(*args, **kwargs):
        assert release.is_set(), "Do not regrade state while live drain is pending"
        calls.append("offline_state")
        return {"status": "measured"}

    monkeypatch.setattr("tau2.diagnostics.collect.EvidenceRecorder", Recorder)
    monkeypatch.setattr("tau2.diagnostics.collect.finalize_evidence", collect)
    monkeypatch.setattr("tau2.diagnostics.state.sidecar.StateShadowWorker", Worker)
    monkeypatch.setattr("tau2.runner.simulation.evaluate_simulation", formal)
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.evaluator.score_persistent_task",
        persistent,
    )
    monkeypatch.setattr("tau2.diagnostics.voice.evaluate_voice", voice)
    monkeypatch.setattr(
        "tau2.diagnostics.state.metrics.evaluate_state_interaction", offline_state
    )
    orchestrator = prepared()
    orchestrator.environment.tools = SimpleNamespace(tools={})
    orchestrator.run().info.pop("scenario_mechanisms")

    def run():
        context.set("preserved")
        return run_simulation(
            orchestrator,
            diagnostics_dir=tmp_path / "diagnostics",
            metrics_config={
                "measures": ["state_interaction", "persistent_task", "voice"],
                "state_sidecar": "online_full",
                "metric_concurrency": concurrency,
            },
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert independent.wait(3), "Independent metrics were blocked by state"
            assert outcomes.wait(3), "Formal grading was blocked by state"
            assert not future.done()
        finally:
            release.set()
        result = future.result(timeout=3)

    assert result.reward_info.reward == 1
    assert result.evaluation_status == "completed"
    scores = result.diagnostics_ref["summary"]["measures"]
    assert all(row["status"] == "measured" for row in scores.values())
    assert scores["persistent_task"]["completion_score"] == 1
    assert calls.count("persistent") == 1
    assert calls.count("offline_state") == (0 if complete else 1)
    assert evidence.model_dump(mode="json") == before
    if complete:
        assert scores["state_interaction"]["source"] == "online_full_sidecar"


def test_exhausted_online_state_leaves_full_budget_for_other_metrics(
    monkeypatch, tmp_path
):
    """Reproduce the 0054 budget failure with real sidecar and suite routing."""
    from tau2.diagnostics.state.models import Interpretation

    state_exhausted = Event()
    workers = []
    independent_budgets = []

    class Recorder:
        def __init__(self, *args):
            self.operations, self.frames, self.errors = [], {}, []

        def set_state_event_sink(self, sink):
            self.sink = sink
            workers.append(sink.__self__)

    orchestrator = prepared()
    simulation = orchestrator.run()
    simulation.info.pop("scenario_mechanisms")
    orchestrator.environment.tools = SimpleNamespace(tools={})

    def run():
        for sequence in range(3):
            orchestrator.environment.diagnostic_recorder.sink(
                {
                    "event_id": f"user:{sequence}",
                    "sequence": sequence,
                    "kind": "user_turn",
                    "role": "user",
                    "content": "Please continue",
                }
            )
        assert state_exhausted.wait(3)
        return simulation

    def interpret(self, event, visible):
        try:
            self.model.budget.take()
        except RuntimeError:
            state_exhausted.set()
            raise
        return Interpretation()

    evidence = DiagnosticEvidence(simulation_id="s", task_id="t", domain="gaia2")

    async def intent(evidence, config, directory, budget):
        budget.take()
        independent_budgets.append(budget)
        return {"status": "complete"}

    def conciseness(evidence, config, directory, output, budget):
        budget.take()
        independent_budgets.append(budget)
        return {"status": "measured"}

    orchestrator.run = run
    monkeypatch.setattr("tau2.diagnostics.collect.EvidenceRecorder", Recorder)
    monkeypatch.setattr(
        "tau2.diagnostics.collect.finalize_evidence", lambda *args: evidence
    )
    monkeypatch.setattr(
        "tau2.diagnostics.state.semantic.SemanticInterpreter.interpret", interpret
    )
    monkeypatch.setattr("tau2.diagnostics.intent.call.evaluate_call", intent)
    monkeypatch.setattr(
        "tau2.diagnostics.conciseness.evaluator.evaluate_feedback", conciseness
    )
    monkeypatch.setattr(
        "tau2.runner.simulation.evaluate_simulation",
        lambda **kwargs: RewardInfo(reward=1),
    )
    monkeypatch.setattr(
        "tau2.metrics.active.persistent_task.evaluator.score_persistent_task",
        lambda *args: {"status": "measured", "completion_score": 1},
    )
    result = run_simulation(
        orchestrator,
        diagnostics_dir=tmp_path / "diagnostics",
        metrics_config={
            "measures": ["state_interaction", "intent", "conciseness"],
            "state_sidecar": "online_full",
            "state_sidecar_stability_seconds": 0,
            "max_requests": 2,
        },
    )
    summary = result.diagnostics_ref["summary"]
    assert summary["measures"]["intent"]["status"] == "complete"
    assert summary["measures"]["conciseness"]["status"] == "measured"
    assert summary["measures"]["state_interaction"]["status"] == "partial"
    state_budget = workers[0].request_budget
    assert state_budget.used == 2 and state_budget.closed
    assert len(independent_budgets) == 2
    assert independent_budgets[0] is independent_budgets[1]
    assert state_budget is not independent_budgets[0]
    assert summary["requests_this_run"] == independent_budgets[0].used == 2

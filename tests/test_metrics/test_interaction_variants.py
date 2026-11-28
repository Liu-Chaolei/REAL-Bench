"""Request changes retain actual authorization, task identity and score scope."""

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau2.data_model.tasks import Task
from tau2.diagnostics.intent.io import digest
from tau2.domains.gaia2.interaction_variants import (
    InteractionVariant,
    apply_variant,
    metric_enabled,
)
from tau2.metrics.active.config import PersistentTaskMetricConfig
from tau2.metrics.active.persistent_task.variant import InteractionVariantRuntime
from tau2.persistent_tasks.core.models import BackendTaskObservation


@pytest.fixture
def source():
    rows = json.loads(Path("data/tau2/domains/gaia2/tasks.json").read_text())
    return Task.model_validate(rows[0])


def authored(source, kind="modify"):
    fields = dict(
        id=source.id + "__" + kind + "_v1",
        source_task_id=source.id,
        source_sha256=digest(source.model_dump(mode="json")),
        variant_type=kind,
        stage=2,
        initial_request="Arrange primary at 14:00",
        followup_request="Use 15:00 instead"
        if kind == "modify"
        else "Cancel temporary only, keep primary",
        temporary_request="Prepare temporary report" if kind == "cancel" else None,
        primary_hint="primary",
        target_hint="temporary" if kind == "cancel" else "primary",
    )
    if kind == "addtask":
        from tau2.data_model.scenario import ScenarioAction

        fields.update(
            reference_answer="Extra",
            reference_events=[
                ScenarioAction(
                    id=source.id + "__addtask_v1:ref",
                    kind="reference",
                    tool="Calendar__add_calendar_event",
                    arguments={"title": "Extra"},
                    dependencies=[source.id + "__addtask_v1:followup"],
                )
            ],
        )
    return InteractionVariant(**fields)


def running(source, kind="modify"):
    variant = authored(source, kind)
    scenario = SimpleNamespace(
        current=0,
        lock=threading.RLock(),
        user_inputs=[],
        listeners=[],
    )
    scenario.time = lambda: scenario.current
    scenario.add_delivery_listener = scenario.listeners.append
    port = SimpleNamespace(events=[])
    port.enable = lambda: None
    port.get_capabilities = lambda: {
        "task_identity": True,
        "operations": ["modify", "cancel"],
    }

    def drain():
        result, port.events = port.events, []
        return result

    port.drain_events = drain
    task = apply_variant(source, variant)
    orchestrator = SimpleNamespace(
        task=task,
        simulation_id="s",
        environment=SimpleNamespace(scenario_runtime=scenario),
        agent=SimpleNamespace(get_async_evidence_source=lambda: port),
        call_context=None,
    )
    config = PersistentTaskMetricConfig(
        preferred_options=[dict(state="running", operation="query", weight=1)]
    )
    metric = InteractionVariantRuntime(
        config, variant=variant, orchestrator=orchestrator, seed=1, trial=0
    )
    metric.install()
    metric.on_delivery("user", variant.initial_request, metric._initial_id(), 0)
    metric.delivery_completed(metric._initial_id(), 1)
    scenario.current = 2
    return metric, port, scenario


def created(task_id, objective, **kwargs):
    return BackendTaskObservation(
        provider="fake",
        kind="task_created",
        timestamp=2,
        backend_task_id=task_id,
        objective=objective,
        status="running",
        **kwargs,
    )


def test_source_view_keeps_reference_and_replaces_only_initial_request(source):
    original = source.model_dump(mode="json")
    variant = authored(source)
    task = apply_variant(source, variant)
    assert source.model_dump(mode="json") == original
    assert task.evaluation_criteria == source.evaluation_criteria
    assert task.scenario.stages[2].request == variant.initial_request
    assert task.scenario.stages[0] == source.scenario.stages[0]
    changed = source.model_copy(deep=True)
    changed.scenario.stages[1].request += " changed"
    with pytest.raises(ValueError, match="source changed"):
        apply_variant(changed, variant)


def test_cancel_waits_for_independent_b_and_never_credits_cancel_a(source):
    metric, port, scene = running(source, "cancel")
    port.events.append(created("A", "primary"))
    metric.advance(2, True)
    assert len(scene.user_inputs) == 1
    assert metric.requests[0]["phase"] == "temporary"
    metric.delivery_completed(metric.variant.request_id("temporary"), 3)
    port.events.append(
        BackendTaskObservation(
            provider="fake",
            kind="native_operation_completed",
            timestamp=4,
            backend_task_id="A",
            operation="cancel",
            result={"effect": "cancelled"},
        )
    )
    metric.advance(4, True)
    assert metric.bindings == {"primary": "A"}
    assert len(scene.user_inputs) == 1
    metric.finalize("user_stop", 5)
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is None


def test_cancel_b_uses_b_result_and_preserves_primary_binding(source):
    metric, port, scene = running(source, "cancel")
    port.events.append(created("A", "primary"))
    metric.advance(2, True)
    temporary = metric.variant.request_id("temporary")
    metric.on_delivery("user", "temporary", temporary, 3)
    metric.delivery_completed(temporary, 4)
    scene.current = 5
    port.events.append(created("B", "temporary", request_id=temporary))
    metric.advance(5, True)
    assert metric.backend_task_id == "B"
    assert metric.bindings == {"primary": "A", "temporary": "B"}
    assert metric.requests[1]["previous_request_id"] == temporary
    request = metric.variant.request_id()
    metric.on_delivery("user", "cancel", request, 6)
    assert metric.delivery["delivered_at"] is None
    metric.delivery_completed(request, 7)
    metric.operations = [
        dict(
            call_id="c",
            backend_task_id="B",
            operation="cancel",
            timestamp=8,
            kind="native_operation_started",
        ),
        dict(
            call_id="c",
            backend_task_id="B",
            operation="cancel",
            timestamp=9,
            kind="native_operation_completed",
            result={"effect": "cancelled"},
        ),
    ]
    metric.reply_text = "The temporary report is cancelled."
    result = metric.score()
    assert result["evaluation_log"]["native_result"]["passed"] is True
    assert result["evaluation_log"]["reply"]["passed"] is True
    metric.operations[1]["backend_task_id"] = "A"
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is False


def test_management_started_before_delivery_cannot_pass_on_late_return(source):
    metric, _, _ = running(source)
    metric.status = "delivered"
    metric.backend_task_id = "A"
    metric.generated_request = {"resolved_intent": {"operation": "modify"}}
    metric.delivery["delivered_at"] = 7
    metric.operations = [
        dict(
            call_id="c",
            backend_task_id="A",
            operation="modify",
            timestamp=6,
            kind="native_operation_started",
        ),
        dict(
            call_id="c",
            backend_task_id="A",
            operation="modify",
            timestamp=8,
            kind="native_operation_completed",
            result={"accepted": True},
        ),
    ]
    metric.reply_text = "Updated."
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is False
    metric.operations[0]["timestamp"] = 7
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is True


def test_terminal_task_never_falls_back_to_query(source):
    metric, port, _ = running(source)
    port.events.append(created("A", "primary"))
    port.events.append(
        BackendTaskObservation(
            provider="fake",
            kind="task_state_changed",
            timestamp=3,
            backend_task_id="A",
            status="completed",
        )
    )
    metric.advance(4, True)
    assert metric.status == "target_finished_before_request"
    assert metric.requests == []
    assert metric.score()["completion_score"] is None


@pytest.mark.parametrize("mode", ["half_duplex", "full_duplex"])
def test_disabled_environment_is_not_called(source, mode, monkeypatch):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import SimulationRun
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.evaluator.evaluator_env import (
        EnvironmentEvaluator,
        FullDuplexEnvironmentEvaluator,
    )

    def forbidden(**kwargs):
        pytest.fail("ENV must be skipped before replay")

    monkeypatch.setattr(EnvironmentEvaluator, "calculate_reward", forbidden)
    monkeypatch.setattr(FullDuplexEnvironmentEvaluator, "calculate_reward", forbidden)
    task = apply_variant(source, authored(source))
    simulation = SimulationRun(
        id="s",
        task_id=task.id,
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason="user_stop",
        messages=[],
        ticks=[],
    )
    result = evaluate_simulation(
        simulation,
        task,
        EvaluationType.ALL,
        False,
        "gaia2",
        mode,
        communicate_scoring=CommunicateScoring(),
    )
    assert result.reward is None
    assert result.reward_breakdown == {}
    assert not metric_enabled(task, "execution")
    assert not metric_enabled(task, "state_interaction")
    assert metric_enabled(task, "intent")
    assert metric_enabled(source, "environment")


def test_disabled_diagnostics_never_construct_their_judges(source, tmp_path):
    from tau2.data_model.diagnostics import DiagnosticEvidence
    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.suite import evaluate_evidence

    task = apply_variant(source, authored(source, "cancel"))
    evidence = DiagnosticEvidence(simulation_id="s", task_id=task.id, domain="gaia2")
    report = evaluate_evidence(
        task,
        evidence,
        tmp_path / "evidence",
        MetricsConfig(measures=["execution", "state_interaction"]),
    )
    summary = report["summary"]
    assert summary["requests_this_run"] == 0
    assert {r["status"] for r in summary["measures"].values()} == {"not_applicable"}


def test_addtask_supplements_reference_without_disabling_business_scores(source):
    from tau2.data_model.scenario import ScenarioAction

    fields = authored(source).model_dump(mode="json")
    fields["initial_request"] = source.scenario.stages[2].request
    fields.update(
        variant_type="addtask",
        scoring_mode="addtask_result",
        reference_answer="A separate appointment is created.",
        reference_events=[
            ScenarioAction(
                id="extra",
                kind="reference",
                tool="Calendar__add_calendar_event",
                arguments={"title": "Extra"},
                dependencies=[fields["id"] + ":followup"],
            )
        ],
    )
    variant = InteractionVariant.model_validate(fields)
    task = apply_variant(source, variant)
    assert task.scenario.events[: len(source.scenario.events)] == source.scenario.events
    assert task.scenario.events[-1].id == "extra"
    assert task.scenario.events[-1].dependencies == [variant.request_id()]
    assert all(
        metric_enabled(task, m)
        for m in ("environment", "execution", "state_interaction")
    )


def test_delta_loader_keeps_source_file_and_exposes_separate_splits(source, tmp_path):
    from tau2.domains.gaia2.environment import get_tasks, get_tasks_split

    path = tmp_path / "tasks.json"
    original = json.dumps([source.model_dump(mode="json")])
    path.write_text(original)
    (tmp_path / "split_tasks.json").write_text(
        json.dumps({"base": [source.id], "all": [source.id]})
    )
    variants = [
        authored(source, kind).model_dump(mode="json") for kind in ("modify", "cancel")
    ]
    (tmp_path / "interaction_variants.json").write_text(json.dumps(variants))
    tasks = get_tasks(root=tmp_path)
    assert len(tasks) == 3
    assert path.read_text() == original
    assert get_tasks_split(root=tmp_path)["cancel"] == [variants[1]["id"]]
    assert len(get_tasks(root=tmp_path, include_interaction_variants=False)) == 1
    assert len(get_tasks("base", root=tmp_path)) == 1
    assert len(get_tasks("modify", root=tmp_path)) == 1


def test_diagnostic_report_separates_the_three_variant_families():
    from tau2.diagnostics.suite import summarize

    rows = [
        dict(
            task_id=kind,
            summary={"source": {"interaction_variant": {"variant_type": kind}}},
        )
        for kind in ("modify", "cancel", "addtask")
    ]
    report = summarize(rows)
    assert set(report["groups"]) == {"modify", "cancel", "addtask"}
    assert all(
        group["evaluation_log"]["total_trials"] == 1
        for group in report["groups"].values()
    )


def test_addtask_followup_is_owned_by_the_backend_trigger(source):
    from tau2.data_model.scenario import ScenarioAction
    from tau2.domains.gaia2.environment import get_environment

    fields = authored(source).model_dump(mode="json")
    fields.update(
        variant_type="addtask",
        initial_request=source.scenario.stages[2].request,
        reference_answer="Extra calendar entry created.",
        reference_events=[
            ScenarioAction(
                id="extra",
                kind="reference",
                tool="Calendar__add_calendar_event",
                arguments={"title": "Extra"},
                dependencies=[fields["id"] + ":followup"],
            )
        ],
    )
    variant = InteractionVariant.model_validate(fields)
    environment = get_environment(
        task=apply_variant(source, variant),
        allow_unverified=True,
        stage_control=True,
        runtime_mode="reference",
    )
    try:
        runtime = environment.scenario_runtime
        assert variant.request_id() in runtime.managed_user_events
        runtime.completed[stage_initial_id(environment.task)] = 0
        runtime.advance(0)
        assert not any(key == variant.request_id() for key, _ in runtime.user_inputs)
    finally:
        environment.close()


def stage_initial_id(task):
    from tau2.orchestrator.scenario_stages import stage_requests

    return stage_requests(task.scenario)[2].id


@pytest.mark.parametrize("accepted", [True, False])
def test_primary_can_bind_accepted_initial_update_to_existing_task(source, accepted):
    metric, port, _ = running(source, "addtask")
    port.events.append(
        BackendTaskObservation(
            provider="fake",
            kind="native_operation_completed",
            timestamp=2,
            backend_task_id="existing-task",
            operation="modify",
            call_id="initial",
            arguments={"instruction": "Arrange primary at 14:00"},
            result={"accepted": accepted},
        )
    )
    metric.advance(2, True)
    assert bool(metric.bindings.get("primary")) == accepted
    assert bool(metric.requests) == accepted
    if accepted:
        metric.delivery_completed(metric.variant.request_id(), 3)
        assert metric.score()["evaluation_log"]["native_result"]["passed"] is None


def test_merging_temporary_work_into_primary_does_not_invent_independent_b(source):
    metric, port, _ = running(source, "cancel")
    port.events.append(created("A", "primary"))
    metric.advance(2, True)
    request = metric.variant.request_id("temporary")
    metric.on_delivery("user", "Prepare temporary report", request, 3)
    metric.delivery_completed(request, 4)
    port.events.append(
        BackendTaskObservation(
            provider="fake",
            kind="native_operation_completed",
            timestamp=5,
            backend_task_id="A",
            operation="modify",
            call_id="merged",
            arguments={"instruction": "Prepare temporary report"},
            result={"accepted": True},
        )
    )
    metric.advance(5, True)
    assert metric.bindings == {"primary": "A"}
    assert metric.binding_status["temporary"] == "merged"
    assert len(metric.requests) == 2
    assert metric.requests[1]["phase"] == "followup"
    metric.delivery_completed(metric.variant.request_id("followup"), 6)
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is None


def test_qwen_started_management_call_has_no_fabricated_native_result():
    from tau2.adapters.qwen_audio_agent.events import QwenEventAdapter

    adapter = QwenEventAdapter()
    started = adapter.backend_observation(
        dict(
            kind="native_operation_started",
            timestamp=1,
            session_id="s",
            task_id="A",
            operation="modify",
            call_id="c",
        )
    )
    assert started.result is None
    completed = adapter.backend_observation(
        dict(
            kind="native_operation_completed",
            timestamp=2,
            session_id="s",
            task_id="A",
            operation="modify",
            call_id="c",
            result={"accepted": True},
        )
    )
    assert completed.result["accepted"] is True


def test_native_score_uses_return_instead_of_placeholder_on_start(source):
    metric, _, _ = running(source)
    metric.status = "delivered"
    metric.backend_task_id = "A"
    metric.generated_request = {"resolved_intent": {"operation": "modify"}}
    metric.delivery["delivered_at"] = 1
    metric.operations = [
        dict(
            call_id="c",
            backend_task_id="A",
            operation="modify",
            timestamp=2,
            kind="native_operation_started",
            result={"accepted": False},
        ),
        dict(
            call_id="c",
            backend_task_id="A",
            operation="modify",
            timestamp=3,
            kind="native_operation_completed",
            result={"accepted": True},
        ),
    ]
    assert metric.score()["evaluation_log"]["native_result"]["passed"] is True

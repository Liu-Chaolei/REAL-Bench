from types import SimpleNamespace

from tau2.metrics.active.config import PersistentTaskMetricConfig
from tau2.metrics.active.persistent_task.generator import GeneratedRequest
from tau2.metrics.active.persistent_task.runtime import PersistentTaskMetricRuntime
from tau2.persistent_tasks.core.models import BackendTaskObservation


class FakeRuntime:
    def __init__(self):
        self.current = 0.0
        self.user_inputs = []
        self.listeners = []

    def time(self):
        return self.current

    def timestamp_at(self, _value):
        return self.current

    def add_delivery_listener(self, listener):
        self.listeners.append(listener)

    def queue_user_input(self, event_id, text, *, source="scenario", purpose="request"):
        self.user_inputs.append((event_id, text))
        self.input_metadata = {"source": source, "purpose": purpose}

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    lock = _Lock()


class FakeSource:
    def __init__(self):
        self.pending = []

    def enable(self):
        return None

    def get_capabilities(self):
        return {
            "task_identity": True,
            "states": ["queued", "running", "completed", "failed", "cancelled"],
            "operations": ["query", "result"],
        }

    def drain_events(self):
        pending, self.pending = self.pending, []
        return pending

    def get_status(self):
        return {"request_boundary": True}


def runtime():
    scenario = FakeRuntime()
    source = FakeSource()
    orchestrator = SimpleNamespace(
        simulation_id="sim-1",
        task=SimpleNamespace(id="task-1"),
        environment=SimpleNamespace(scenario_runtime=scenario),
        agent=SimpleNamespace(get_async_evidence_source=lambda: source),
        call_context=None,
    )
    config = PersistentTaskMetricConfig.model_validate(
        {
            "preferred_options": [
                {"state": "running", "operation": "query", "weight": 1}
            ],
            "gates": {
                "min_observations_after_creation": 2,
                "min_dwell_seconds": 1,
                "min_turn_boundaries": 1,
            },
        }
    )
    metric = PersistentTaskMetricRuntime(
        config, orchestrator=orchestrator, seed=1, trial=0
    )
    metric.generator.generate = lambda _context: GeneratedRequest(
        "Please check the task status.",
        "template-fallback",
        "en",
        "running",
        "query",
    )
    metric.install()
    return metric, source, scenario


def event(index, status, timestamp):
    return BackendTaskObservation(
        provider="fake",
        kind="task_state_changed",
        timestamp=timestamp,
        backend_task_id="backend-1",
        status=status,
        event_id=str(index),
        sequence=index,
        objective="the task",
    )


def test_dynamic_request_is_queued_after_stable_preferred_state():
    metric, source, scenario = runtime()
    for index, status in enumerate(("queued", "running", "running")):
        scenario.current = index * 2
        source.pending.append(event(index, status, scenario.current))
        metric.advance(scenario.current, True)

    assert metric.status == "queued"
    assert scenario.user_inputs[0][0].startswith("persistent_task_metric_")
    assert scenario.user_inputs[0][1] == "Please check the task status."
    assert scenario.input_metadata == {
        "source": "active_persistent_task",
        "purpose": "followup",
    }


def test_failed_state_uses_deterministic_fallback():
    metric, source, scenario = runtime()
    for index, status in enumerate(("queued", "failed")):
        scenario.current = index * 2
        source.pending.append(event(index, status, scenario.current))
        metric.advance(scenario.current, True)
    scenario.current = 3
    metric.advance(scenario.current, True)

    assert metric.status == "queued"
    assert metric.generated_request["resolved_intent"]["state"] == "failed"
    assert metric.generated_request["resolved_intent"]["fallback_used"] is True


def test_missing_language_template_reports_generation_unavailable(monkeypatch):
    metric, source, scenario = runtime()
    monkeypatch.setattr(
        metric.generator,
        "generate",
        lambda _context: (_ for _ in ()).throw(RuntimeError("llm_timeout")),
    )
    metric.config.language = "zz"
    for index, status in enumerate(("queued", "running", "running")):
        scenario.current = index * 2
        source.pending.append(event(index, status, scenario.current))
        metric.advance(scenario.current, True)

    assert metric.status == "generation_unavailable"


def test_reply_score_still_uses_native_result_evidence(monkeypatch):
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import CommunicateCheck
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

    metric, _, _ = runtime()
    metric.status = "delivered"
    metric.backend_task_id = "backend-1"
    metric.generated_request = {"resolved_intent": {"operation": "query"}}
    metric.delivery = {"delivered_at": 1.0}
    metric.operations = [
        {
            "backend_task_id": "backend-1",
            "operation": "query",
            "timestamp": 2.0,
            "success": True,
            "result": {"state": "running"},
        }
    ]
    metric.reply_text = "It is still running."
    calls = []

    def judge(messages, facts, **kwargs):
        calls.append((messages, facts))
        return [
            CommunicateCheck(
                info=facts[0], met=True, justification="Matches native state"
            )
        ]

    monkeypatch.setattr(CommunicateEvaluator, "evaluate_communicate_info", judge)
    result = metric.score(CommunicateScoring())
    assert len(calls) == 1 and '"actual_state": "running"' in calls[0][1][0]
    assert (
        result["evaluation_log"]["native_result"]["passed"]
        == result["evaluation_log"]["reply"]["passed"]
        == result["completion_score"]
        == 1
    )
    assert "state_result_reward" not in result

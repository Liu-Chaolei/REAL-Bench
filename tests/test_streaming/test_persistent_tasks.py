"""Full streaming control flow with native observation fixtures, without APIs."""

import time

import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec, ScenarioStage
from tau2.data_model.tasks import Task, UserScenario
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.scenario import ScenarioRuntime
from tau2.environment.toolkit import ToolKitBase
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.persistent_tasks.backend import BackendTaskObservation
from tau2.persistent_tasks.backend_runtime import replay
from tau2.persistent_tasks.integration import install


class FixtureEvidenceSource:
    """Non-Qwen Agent fixture implementing the same passive evidence port."""

    def __init__(self, agent):
        self.agent = agent

    def enable(self):
        pass

    def get_capabilities(self):
        return {
            "task_identity": True,
            "generated_reply": True,
            "multi_task": True,
            "states": ["queued", "running", "completed", "failed", "cancelled"],
            "operations": ["query", "result", "modify", "cancel"],
        }

    def get_status(self):
        return {
            "ready": True,
            "request_boundary": True,
            "evidence_gap": False,
            "capabilities": self.get_capabilities(),
            "final_drain_complete": True,
        }

    def drain_events(self):
        return self.agent.drain_backend_observations()


@pytest.mark.parametrize("authorization_followup", [False, True])
@pytest.mark.parametrize("initial_prefix", [None, "Track this work explicitly."])
def test_running_query_crosses_a_real_input_queue_without_stage_stop_deadlock(
    authorization_followup,
    initial_prefix,
):
    scene = ScenarioSpec(
        start_time=1000,
        source_id="source",
        source_sha256="0" * 64,
        snapshot="0" * 64,
        category="fixture",
        events=[
            ScenarioAction(
                id="initial",
                kind="user",
                tool="user",
                arguments={"content": "Do the work"},
            )
        ],
        stages=[
            ScenarioStage(
                stage=0, request="Do the work", event_ids=["initial"], criteria={}
            )
        ],
    )
    environment = Environment("gaia2", "policy", tools=ToolKitBase(DB()))
    environment.scenario_runtime = ScenarioRuntime(scene, lambda *a: None)

    class Caller:
        scenario_transition_pending = False

        def __init__(self):
            self.inputs = []
            self.delivered = []
            self.spoken = []
            self.authorized = False

        def queue_scenario_input(self, key, text):
            self.inputs.append((key, text))

        def get_init_state(self):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if self.inputs:
                key, text = self.inputs.pop(0)
                self.delivered.append(key)
                self.spoken.append(text)
                return UserMessage(
                    role="user",
                    content=text,
                    contains_speech=True,
                    is_final_chunk=True,
                    raw_data={"scenario_input_id": key},
                ), state
            if (
                authorization_followup
                and not self.authorized
                and participant_chunk
                and participant_chunk.content == "The task is still running."
            ):
                self.authorized = True
                return UserMessage(
                    role="user",
                    content="Go ahead",
                    contains_speech=True,
                    is_final_chunk=True,
                    utterance_ids=["authorization"],
                ), state
            # Exercise the STOP that previously set transition_pending and made
            # the user refuse to play its persistent input indefinitely.
            return UserMessage(
                role="user", content="###STOP###", contains_speech=False
            ), state

        def stop(self, *args, **kwargs):
            pass

    class Agent:
        provider = "fixture_agent"
        has_pending_output = False

        def __init__(self):
            self.events = []
            self.tick = 0
            self.reply_pending = False

        def get_async_evidence_source(self):
            return FixtureEvidenceSource(self)

        def enable_backend_observation(self):
            pass

        def backend_observation_status(self):
            return {
                "request_boundary": True,
                "evidence_gap": False,
                "capabilities": {"version": 1},
            }

        def drain_backend_observations(self):
            events, self.events = self.events, []
            return events

        def get_init_state(self, **kwargs):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            self.tick += 1
            text = "The task is still running." if self.reply_pending else None
            self.reply_pending = False
            if participant_chunk and (participant_chunk.content or "").endswith(
                "Do the work"
            ):
                self.events.append(
                    BackendTaskObservation(
                        provider="qwen",
                        kind="task_created",
                        timestamp=0,
                        received_monotonic=time.monotonic(),
                        backend_task_id="native",
                        status="running",
                    )
                )
                text = "Starting."
            elif participant_chunk and participant_chunk.content == "Status?":
                for kind in ("native_operation_started", "native_operation_completed"):
                    self.events.append(
                        BackendTaskObservation(
                            provider="qwen",
                            kind=kind,
                            timestamp=0,
                            received_monotonic=time.monotonic(),
                            backend_task_id="native",
                            call_id="query-1",
                            operation="query",
                            success=True,
                            result={"status": "ok", "task_status": "running"},
                        )
                    )
                self.reply_pending = True
            elif participant_chunk and participant_chunk.content == "Go ahead":
                for kind in ("native_operation_started", "native_operation_completed"):
                    self.events.append(
                        BackendTaskObservation(
                            provider="qwen",
                            kind=kind,
                            timestamp=0,
                            received_monotonic=time.monotonic(),
                            backend_task_id="native",
                            call_id="authorization-update",
                            operation="modify",
                            success=True,
                            result={"status": "accepted"},
                        )
                    )
            raw = None
            if text:
                raw = {
                    "live_transcript": {
                        "origin": "recorded_audio",
                        "alignment": "played_audio",
                        "fragments": [
                            {
                                "event_id": f"reply-{self.tick}",
                                "text": text,
                                "start_ms": self.tick - 1,
                                "end_ms": self.tick,
                            }
                        ],
                    }
                }
            return AssistantMessage(
                role="assistant", content=text, contains_speech=bool(text), raw_data=raw
            ), state

        def is_stop(self, message):
            return False

        def stop(self, *args, **kwargs):
            pass

        def get_final_live_transcript(self):
            return None, None

    task = Task(
        id="source",
        scenario=scene,
        user_scenario=UserScenario(instructions="Do the work"),
    )
    caller, agent = Caller(), Agent()
    orchestrator = FullDuplexOrchestrator(
        "gaia2",
        agent,
        caller,
        environment,
        task,
        max_steps=30,
        tick_duration_seconds=0.001,
    )
    install(
        orchestrator,
        {
            "scenario_id": "fixture",
            "initial_request_prefix": initial_prefix,
            "bindings": [{"source_task_id": "source", "source_stage": 0}],
            "requests": [
                {
                    "request_id": "r1",
                    "text": "Status?",
                    "state": "running",
                    "delay_seconds": 0,
                    "reply_expectation": "Report status",
                }
            ],
        },
    )
    result = orchestrator.run()
    assert caller.delivered == ["initial", "r1"]
    assert (
        caller.spoken[0]
        == ((initial_prefix + "\n\n") if initial_prefix else "") + "Do the work"
    )
    assert task.scenario.events[0].arguments["content"] == "Do the work"
    summary = result.info["persistent_tasks"]
    assert summary["releases"]["r1"]["delivered_at"] is not None
    assert summary["operations"][0]["request_id"] == "r1"
    assert summary["operations"][0]["identity"]["backend_task_id"] == "native"
    assert "still running" in summary["reply_windows"]["r1"]["text"]
    if authorization_followup:
        assert summary["operations"][1]["request_id"] is None
        assert summary["reply_windows"]["r1"]["end"] < summary["closed_at"]
    assert replay(summary)["releases"] == summary["releases"]


def test_business_completion_from_worker_is_only_applied_on_orchestration_thread():
    from threading import Thread
    from types import SimpleNamespace

    from tau2.persistent_tasks.integration import advance

    scene = ScenarioSpec(
        start_time=1000,
        source_id="source",
        source_sha256="0" * 64,
        snapshot="0" * 64,
        category="fixture",
        events=[
            ScenarioAction(
                id="initial", kind="user", tool="user", arguments={"content": "Work"}
            ),
            ScenarioAction(
                id="anchor", kind="reference", tool="unused", dependencies=["initial"]
            ),
        ],
        stages=[
            ScenarioStage(
                stage=0, request="Work", event_ids=["initial", "anchor"], criteria={}
            )
        ],
    )
    runtime = ScenarioRuntime(scene, lambda *a: None)
    agent = SimpleNamespace(
        provider="qwen_agent",
        enable_backend_observation=lambda: None,
        drain_backend_observations=lambda: [],
    )
    agent.get_async_evidence_source = lambda: FixtureEvidenceSource(agent)
    task = Task(
        id="source", scenario=scene, user_scenario=UserScenario(instructions="Work")
    )
    orchestrator = SimpleNamespace(
        domain="gaia2",
        agent=agent,
        user=SimpleNamespace(),
        environment=SimpleNamespace(scenario_runtime=runtime),
        task=task,
        call_context=None,
    )
    coordinator = install(
        orchestrator,
        {
            "scenario_id": "worker",
            "bindings": [
                {
                    "source_task_id": "source",
                    "source_stage": 0,
                    "activation_event_ids": ["anchor"],
                }
            ],
            "requests": [
                {
                    "request_id": "r1",
                    "text": "Status?",
                    "state": "running",
                    "reply_expectation": "Report status",
                }
            ],
        },
    )
    try:
        runtime.start()
        worker = Thread(
            target=lambda: runtime._event_completion_listeners[0](
                "anchor", {"done": True}, runtime.time()
            )
        )
        worker.start()
        worker.join()
        assert "anchor" not in coordinator.binder.completed_events
        advance(orchestrator, False)
        assert "anchor" in coordinator.binder.completed_events
    finally:
        runtime.close()

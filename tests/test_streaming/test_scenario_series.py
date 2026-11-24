"""Continuous conversations advance on user STOP without scoring or reconnecting."""

from types import SimpleNamespace

import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec, ScenarioStage
from tau2.data_model.semantic_communication import CommunicateScoring
from tau2.data_model.tasks import Task, UserScenario
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.scenario import ScenarioRuntime
from tau2.environment.toolkit import ToolKitBase
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.orchestrator.scenario_stages import ScenarioStages


def spec():
    events = [
        ScenarioAction(
            id=f"user{i}",
            kind="user",
            tool="user",
            arguments={"content": f"Request {i}"},
            dependencies=[f"ref{i - 1}"] if i else [],
        )
        for i in range(3)
    ]
    events += [
        ScenarioAction(
            id=f"ref{i}", kind="reference", tool="unused", dependencies=[f"user{i}"]
        )
        for i in range(3)
    ]
    return ScenarioSpec(
        start_time=1000,
        source_id="test",
        source_sha256="0" * 64,
        snapshot="0" * 64,
        category="test",
        events=events,
        stages=[
            ScenarioStage(
                stage=i,
                request=f"Request {i}",
                event_ids=[f"user{i}", f"ref{i}"],
                criteria={"communicate_info": [f"Answer {i}"]},
            )
            for i in range(3)
        ],
    )


def test_stop_is_not_success_and_duplicate_stop_cannot_skip_a_request():
    scene = spec()
    runtime = ScenarioRuntime(scene, lambda *a: None)
    controller = ScenarioStages(scene)
    stop = UserMessage(role="user", content="###STOP###")
    controller.delivered("user0", runtime, 0, complete=True)
    assert controller.stop(stop, runtime, 1)
    assert controller.stop(stop, runtime, 2)
    user = SimpleNamespace(queue_scenario_input=lambda *a: None)
    controller.release(user, runtime, 3)
    assert controller.current == 1
    assert controller.stop(stop, runtime, 4)
    assert not controller.transition_pending
    controller.delivered("user1", runtime, 5, complete=True)
    assert controller.stop(
        UserMessage(role="user", content="###OUT-OF-SCOPE###"), runtime, 7
    )
    assert controller.transition_pending
    assert controller.current == 1
    assert runtime.results == {}  # No reference effect is invented to advance.


def test_out_of_scope_is_terminal_only_after_the_last_stage():
    scene = spec()
    runtime = ScenarioRuntime(scene, lambda *a: None)
    controller = ScenarioStages(scene)
    controller.current = len(controller.requests) - 1
    controller.delivered("user2", runtime, 0, complete=True)

    assert not controller.stop(
        UserMessage(role="user", content="###OUT-OF-SCOPE###"), runtime, 1
    )


def test_voice_three_stops_preserve_session_and_deliver_each_request_once():
    scene = spec()
    environment = Environment("mock", "policy", tools=ToolKitBase(DB()))
    runtime = ScenarioRuntime(
        scene, lambda *a: None, managed_user_events={"user1", "user2"}
    )
    environment.scenario_runtime = runtime

    class Caller:
        def __init__(self):
            self.inputs = []
            self.init_count = 0

        def queue_scenario_input(self, key, text):
            self.inputs.append((key, text))

        def get_init_state(self):
            self.init_count += 1
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if self.inputs:
                key, text = self.inputs.pop(0)
                return UserMessage(
                    role="user",
                    content=text,
                    contains_speech=True,
                    is_final_chunk=True,
                    raw_data={"scenario_input_id": key},
                ), state
            text = (
                "###STOP###"
                if participant_chunk and participant_chunk.content == "Done"
                else None
            )
            return UserMessage(role="user", content=text, contains_speech=False), state

        def stop(self, *a, **kw):
            pass

    class Agent:
        has_pending_output = False

        def __init__(self):
            self.heard = []
            self.init_count = 0

        def get_init_state(self, **kw):
            self.init_count += 1

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if participant_chunk and participant_chunk.content:
                assert "###STOP###" not in participant_chunk.content
                self.heard.append(participant_chunk.content)
                return AssistantMessage(
                    role="assistant", content="Done", contains_speech=True
                ), state
            return AssistantMessage(
                role="assistant", content=None, contains_speech=False
            ), state

        def is_stop(self, message):
            return False

        def stop(self, *a, **kw):
            pass

    caller, agent = Caller(), Agent()
    task = Task(
        id="series",
        scenario=scene,
        user_scenario=UserScenario(instructions="Do the requests"),
    )
    orchestrator = FullDuplexOrchestrator(
        "mock",
        agent,
        caller,
        environment,
        task,
        max_steps=40,
        tick_duration_seconds=0.001,
    )
    result = orchestrator.run()
    assert result.termination_reason.value == "user_stop"
    assert agent.heard == ["Request 0", "Request 1", "Request 2"]
    assert agent.init_count == caller.init_count == 1
    summary = result.info["scenario_series"]
    assert not summary["not_reached"]
    assert all(row["completed"] for row in summary["stages"])
    assert [len(row["stops"]) for row in summary["stages"]] == [1, 1, 1]
    assert not any(key.startswith("ref") for key in runtime.completed)


def test_joint_communication_uses_playback_time_even_when_asr_returns_late():
    from tau2.evaluator.scenario_series import communication_reward

    scene = spec()
    task = Task(
        id="series",
        scenario=scene,
        user_scenario=UserScenario(instructions="Do requests"),
    )
    summary = {"stages": [{"started": {"time": t}} for t in [10, 20, 30]]}
    # Answer 0 is late in the sequence but its played audio belongs to stage 0.
    records = [
        {"kind": "agent", "time": 22, "result": "Answer 1"},
        {"kind": "agent", "time": 19, "result": "Answer 0"},
        {"kind": "agent", "time": 33, "result": "Answer 2"},
    ]
    scoring = CommunicateScoring(scorer="substring")
    result = communication_reward(task, records, summary, scoring, None, {})
    assert result.reward == 1
    records[1]["time"] = 25  # Saying it in a later stage cannot repair stage 0.
    assert communication_reward(task, records, summary, scoring, None, {}).reward == 0


def test_text_three_stops_continue_in_the_same_user_and_agent(monkeypatch):
    from tau2.agent.llm_agent import LLMAgent
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.user.user_simulator import UserSimulator

    scene = spec()
    environment = Environment("mock", "policy", tools=ToolKitBase(DB()))
    runtime = ScenarioRuntime(
        scene, lambda *a: None, managed_user_events={"user1", "user2"}
    )
    environment.scenario_runtime = runtime
    user = UserSimulator(llm="unused", instructions="Request 0")
    agent = LLMAgent(tools=[], domain_policy="policy", llm="unused")
    heard = []

    def reply(message, state):
        heard.append(message.content)
        return AssistantMessage(role="assistant", content="Done"), state

    monkeypatch.setattr(agent, "generate_next_message", reply)
    monkeypatch.setattr(
        user,
        "_generate_next_message",
        lambda message, state: UserMessage(role="user", content="###STOP###"),
    )
    task = Task(
        id="series",
        scenario=scene,
        user_scenario=UserScenario(instructions="Request 0"),
    )
    result = Orchestrator("mock", agent, user, environment, task, max_steps=20).run()
    assert heard == ["Request 0", "Request 1", "Request 2"]
    assert result.termination_reason.value == "user_stop"
    assert all(row["completed"] for row in result.info["scenario_series"]["stages"])


def test_managed_request_does_not_disable_original_timed_events():
    scene = spec()
    scene.events.append(
        ScenarioAction(
            id="arrival",
            kind="environment",
            tool="notify",
            dependencies=["user0"],
            delay_seconds=5,
        )
    )
    effects = []
    runtime = ScenarioRuntime(
        scene,
        lambda name, args: effects.append(name),
        managed_user_events={"user1", "user2"},
    )
    runtime.advance(0)
    runtime.delivered("user", "Request 0", event_id="user0")
    runtime.advance(5)
    assert effects == ["notify"]
    assert [key for key, _ in runtime.user_inputs] == ["user0"]
    assert not any(key.startswith("ref") for key in runtime.completed)


def test_interrupted_request_cannot_consume_stop_for_the_following_stage():
    scene = spec()
    runtime = ScenarioRuntime(scene, lambda *a: None)
    controller = ScenarioStages(scene)
    controller.delivered("user0", runtime, 0, complete=True)
    assert controller.stop(UserMessage(role="user", content="###STOP###"), runtime, 1)
    controller.release(
        SimpleNamespace(queue_scenario_input=lambda *a: None), runtime, 2
    )
    controller.delivered("user1", runtime, 3)
    controller.interrupted("user1", runtime, 4)
    assert controller.rows[1]["interruptions"]
    assert controller.stop(UserMessage(role="user", content="###STOP###"), runtime, 5)
    assert not controller.transition_pending
    controller.delivered("user1", runtime, 6, complete=True)
    assert controller.stop(UserMessage(role="user", content="###STOP###"), runtime, 7)
    assert controller.transition_pending


def test_missing_last_request_is_logged_without_overriding_business_score(monkeypatch):
    from tau2.data_model.simulation import SimulationRun
    from tau2.data_model.tasks import EvaluationCriteria, RewardType
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation

    scene = spec()
    task = Task(
        id="series",
        scenario=scene,
        user_scenario=UserScenario(instructions="Requests"),
        evaluation_criteria=EvaluationCriteria(reward_basis=[RewardType.ENV_ASSERTION]),
    )
    from tau2.data_model.simulation import RewardInfo

    monkeypatch.setattr(
        "tau2.evaluator.evaluator.EnvironmentEvaluator.calculate_reward",
        lambda **kw: RewardInfo(reward=1.0),
    )
    summary = ScenarioStages(scene).summary("user_stop")
    result = SimulationRun(
        id="test",
        task_id="series",
        start_time="start",
        end_time="end",
        duration=1,
        termination_reason="user_stop",
        info={"scenario_series": summary},
    )
    reward = evaluate_simulation(
        result,
        task,
        EvaluationType.ALL,
        False,
        "mock",
        communicate_scoring=CommunicateScoring(scorer="substring"),
    )
    assert reward.reward == 1
    assert result.info["evaluation_log"]["missing_requests"]


def test_process_judge_receives_boundaries_and_actual_prohibited_operation(monkeypatch):
    from tau2.data_model.tasks import EvaluationCriteria, RewardType
    from tau2.evaluator.evaluator_nl_assertions import NLAssertionsEvaluator

    task = Task(
        id="series",
        scenario=spec(),
        user_scenario=UserScenario(instructions="Requests"),
        evaluation_criteria=EvaluationCriteria(
            nl_assertions=["Do not buy during stage 1"],
            reward_basis=[RewardType.NL_ASSERTION],
        ),
    )

    def judge(**kwargs):
        prompt = kwargs["messages"][-1].content
        assert '"time": 22' in prompt
        assert '"name": "purchase"' in prompt
        assert "Continuous task stage boundaries" in prompt
        return AssistantMessage(
            role="assistant",
            content='{"results":[{"assertion_id":"a0","metExpectation":false,"reasoning":"Purchased during stage 1"}]}',
        )

    monkeypatch.setattr("tau2.evaluator.evaluator_nl_assertions.generate", judge)
    reward = NLAssertionsEvaluator.calculate_reward(
        task,
        [],
        environment_trace=[
            {"kind": "tool", "time": 22, "name": "purchase", "result": "order"}
        ],
        scenario_series={"stages": [{"stage": 1, "started": {"time": 20}}]},
    )
    assert reward.reward == 0


def test_cancelled_script_is_requeued_in_the_same_stage():
    from tau2.data_model.message import TurnTakingAction

    scene = spec()
    runtime = ScenarioRuntime(scene, lambda *args: None)
    controller = ScenarioStages(scene)
    controller.delivered("user0", runtime, 0)
    queued = []
    orchestrator = object.__new__(FullDuplexOrchestrator)
    orchestrator.scenario_stages = controller
    orchestrator.user = SimpleNamespace(
        queue_scenario_input=lambda *args: queued.append(args)
    )
    orchestrator.ticks = []
    orchestrator.tick_duration_seconds = 0.2
    orchestrator._scenario_user_input = "user0"
    orchestrator._scenario_user_text = "Request"
    orchestrator._scenario_user_utterance = ("partial",)
    orchestrator._scenario_last_user_time = 1000
    orchestrator._scenario_audio_ms = [0]
    orchestrator._scenario_audio_times = [1000]
    chunk = UserMessage(
        role="user",
        content=None,
        contains_speech=False,
        turn_taking_action=TurnTakingAction(action="stop_talking"),
    )
    orchestrator._observe_scenario_delivery(runtime, chunk, None)
    assert queued == [("user0", "Request 0")]
    assert controller.current == 0
    assert controller.rows[0]["interruptions"]


def test_scenario_transcript_uses_actual_playback_clock():
    """One snapshot may contain more audio than its nominal 200 ms tick."""
    orchestrator = object.__new__(FullDuplexOrchestrator)
    orchestrator.tick_duration_seconds = 0.2
    orchestrator._scenario_audio_ms = [0]
    orchestrator._scenario_audio_times = [0]
    orchestrator._scenario_seen_fragments = set()
    delivered = []
    runtime = SimpleNamespace(
        started=True,
        monotonic=lambda: 43.0,
        timestamp_at=lambda wall: 1000 + wall,
        delivered=lambda *args, **kwargs: delivered.append((args, kwargs)),
    )
    raw = {
        "played_stream_start_ms": 0,
        "played_stream_duration_ms": 1000,
        "playback_start_monotonic": 41.0,
        "media_cutoff": 42.0,
        "live_transcript": {
            "origin": "recorded_audio",
            "alignment": "played_audio",
            "fragments": [{"event_id": "heard", "text": "Done.", "end_ms": 1000}],
        },
    }
    orchestrator._observe_scenario_delivery(
        runtime, None, AssistantMessage(role="assistant", raw_data=raw)
    )
    assert orchestrator._scenario_audio_ms == [0, 1000]
    assert delivered == [(("agent", "Done."), {"delivered_at": 1042.0})]
    raw.update(
        played_stream_start_ms=1000, played_stream_duration_ms=200, media_cutoff=42.2
    )
    raw["live_transcript"]["fragments"] = [
        {"event_id": "future", "text": "Future.", "end_ms": 1500}
    ]
    with pytest.raises(ValueError, match="audio not yet played"):
        orchestrator._observe_scenario_delivery(
            runtime, None, AssistantMessage(role="assistant", raw_data=raw)
        )

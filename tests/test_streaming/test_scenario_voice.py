"""Timed scenarios use the ordinary streaming simulator and orchestrator."""

import time

import pytest

from tau2.data_model.audio import AudioEncoding, AudioFormat, audio_bytes_to_string
from tau2.data_model.message import (
    AssistantMessage,
    ToolCall,
    TurnTakingAction,
    UserMessage,
)
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.data_model.tasks import Task, UserScenario
from tau2.data_model.voice import SynthesisConfig, VoiceSettings
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.scenario import ScenarioRuntime
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.user.user_simulator_base import UserError
from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator


@pytest.fixture(autouse=True)
def offline_background_speech(monkeypatch):
    """Control-flow tests must not pre-generate background speech via live APIs."""
    monkeypatch.setattr(
        "tau2.voice.synthesis.audio_effects.speech_generator.OutOfTurnSpeechGenerator.generate_all",
        lambda *args, **kwargs: None,
    )


@pytest.mark.parametrize(
    "content",
    ["###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###", "Thanks. ###STOP###"],
)
def test_control_signals_bypass_grounding(monkeypatch, content):
    """The saved 0304 STOP failure must not invoke a judge or synthesize speech."""
    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Ask for help.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=False,
        use_llm_backchannel=False,
    )
    user.scenario_constraints = "Preserve the original request."
    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content=content, cost=0.25, generation_time_seconds=0.1
        ),
    )
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: pytest.fail("Control signals must not be sent to grounding"),
    )
    monkeypatch.setattr(
        user,
        "synthesize_voice",
        lambda *args, **kw: pytest.fail("Control signals must not be spoken"),
    )

    message, state = user._generate_full_duplex_voice_message(
        None, user.get_init_state()
    )

    assert message.content == content
    assert message.cost == 0.25
    assert user.is_stop(message)
    assert not message.has_audio_content()
    assert state._llm_generation_seconds == 0.1


def test_ordinary_voice_reply_still_requires_grounding(monkeypatch):
    """An ordinary invalid candidate still follows the existing grounding policy."""
    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Book October 21.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=False,
        use_llm_backchannel=False,
    )
    user.scenario_constraints = "Book October 21."
    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content="Book October 22 instead."
        ),
    )
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content='{"valid":false,"reason":"Changed the date"}'
        ),
    )

    with pytest.raises(UserError, match="Changed the date"):
        user._generate_full_duplex_voice_message(None, user.get_init_state())


def test_voice_candidate_review_uses_heard_fragments_before_any_tts(monkeypatch):
    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Perform the original task.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=False,
        use_llm_backchannel=False,
    )
    user.scenario_constraints = "Perform the task; no deadline supplied."
    state = user.get_init_state()
    fragments = [
        {
            "event_id": "heard-0",
            "speaker": "agent",
            "text": "I'll tell you ",
            "start_ms": 0,
            "end_ms": 200,
        },
        {
            "event_id": "heard-1",
            "speaker": "agent",
            "text": "tomorrow",
            "start_ms": 200,
            "end_ms": 400,
        },
    ]
    from tau2.data_model.transcript import ParticipantTick

    state.ticks = [
        ParticipantTick(
            tick_id=0,
            timestamp="start",
            self_chunk=UserMessage(
                role="user", content="Please do the task.", contains_speech=True
            ),
        )
    ]
    state.ticks.extend(
        ParticipantTick(
            tick_id=i + 1,
            timestamp="agent",
            other_chunk=AssistantMessage(
                role="assistant",
                content=fragment["text"],
                contains_speech=True,
                raw_data={"live_transcript": {"fragments": [fragment]}},
            ),
        )
        for i, fragment in enumerate(fragments)
    )
    combined = state.get_linearized_messages()
    assert any("tomorrow" in (m.content or "") and m.raw_data is None for m in combined)
    candidates = iter(["Finish today!", "Please continue the original task."])
    decisions = iter([False, True])
    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.generate",
        lambda **kw: AssistantMessage(
            role="assistant", content=next(candidates), cost=0.1
        ),
    )
    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: AssistantMessage(
            role="assistant",
            content=__import__("json").dumps(
                {
                    "valid": next(decisions),
                    "reason": "No deadline",
                    "issue_kind": "time",
                    "heard_evidence": "tomorrow",
                }
            ),
            cost=0.1,
        ),
    )
    reviews, spoken = [], []

    def review(quote, pieces):
        reviews.append((quote, pieces))
        return {"status": "ok", "review_text": "the moment"}

    def synthesize(message, *a, **kw):
        spoken.append(message.content)
        return message

    user.audio_recheck = review
    monkeypatch.setattr(user, "synthesize_voice", synthesize)
    result, _ = user._generate_full_duplex_voice_message(None, state)
    assert reviews == [("tomorrow", fragments)]
    assert spoken == ["Please continue the original task."]
    assert len(result.raw_data["user_recovery"]) == 2


def test_verbatim_input_uses_existing_tts_and_waits_through_agent_speech(monkeypatch):
    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Request an appointment.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=True,
        use_llm_backchannel=False,
    )
    state = user.get_init_state()
    text = "Schedule a one-hour appointment on October 21, 2024."
    calls = []

    def tts(message, state, **kwargs):
        calls.append(message.content)
        return message.model_copy(
            update={
                "is_audio": True,
                "audio_content": audio_bytes_to_string(b"\x01\x00" * 1600),
                "audio_format": AudioFormat(
                    encoding=AudioEncoding.PCM_S16LE, sample_rate=16000
                ),
                "audio_script_gold": message.content,
            }
        )

    monkeypatch.setattr(user, "synthesize_voice", tts)
    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.generate",
        lambda **kw: pytest.fail("Scripted opening must not call an LLM"),
    )
    user.queue_scenario_input("request", text)
    try:
        user._start_generation(
            state,
            action=TurnTakingAction(
                action="generate_message", generation_origin="normal"
            ),
        )
        pending = user._generation_task
        pending.future.result(timeout=2)
        assert pending.origin == "scripted"
        state.agent_input_revision += 1
        state.agent_speech_active = True
        assert user._invalid_voice_task(pending, state) is None
        assert user._next_turn_taking_action(state).action == "wait"
        state.agent_speech_active = False
        action = user._next_turn_taking_action(state)
        message, _ = user._take_generation(state, action)
        assert calls == [text]
        assert message.content == text
        assert message.raw_data["scenario_input_id"] == "request"
        assert user._scenario_inputs == []
    finally:
        user._user_executor.shutdown(wait=True)


def test_tool_wait_does_not_block_full_duplex_ticks():
    class Toolkit(ToolKitBase):
        @is_tool(ToolType.READ)
        def waiting(self):
            """Wait for an environment notification."""
            return runtime.wait_for_notification(0.5)

        waiting.__waits_for_environment__ = True

    scene = ScenarioSpec(
        start_time=1000,
        source_id="fixture",
        source_sha256="0" * 64,
        snapshot="1" * 64,
        category="fixture",
        events=[
            ScenarioAction(
                id="request",
                kind="user",
                tool="user",
                arguments={"content": "Wait for an update"},
            ),
            ScenarioAction(
                id="update", kind="environment", tool="change", delay_seconds=0.06
            ),
        ],
    )
    task = Task(
        id="timed",
        scenario=scene,
        user_scenario=UserScenario(instructions="Wait for an update"),
    )
    environment = Environment("mock", "Fixture policy", tools=Toolkit(DB()))
    runtime = ScenarioRuntime(scene, lambda *args: "arrived")
    environment.scenario_runtime = runtime

    class Caller:
        def __init__(self):
            self.inputs = []

        def queue_scenario_input(self, key, text):
            self.inputs.append((key, text))

        def get_init_state(self):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            if self.inputs:
                key, text = self.inputs.pop(0)
                return UserMessage(
                    role="user",
                    content=text,
                    contains_speech=True,
                    raw_data={"scenario_input_id": key},
                ), state
            return UserMessage(role="user", content=None, contains_speech=False), state

    class Agent:
        def __init__(self):
            self.calls = 0
            self.received = None

        def get_init_state(self, **kwargs):
            return None

        def get_next_chunk(self, state, participant_chunk=None, tool_results=None):
            self.calls += 1
            time.sleep(0.005)
            if tool_results is not None:
                self.received = tool_results
                return AssistantMessage(
                    role="assistant", content="###STOP###", contains_speech=False
                ), state
            if self.calls == 1:
                return AssistantMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="wait",
                            name="waiting",
                            arguments={},
                            requestor="assistant",
                        )
                    ],
                    contains_speech=False,
                ), state
            return AssistantMessage(
                role="assistant", content=None, contains_speech=False
            ), state

        def is_stop(self, message):
            return message.content == "###STOP###"

    agent = Agent()
    orchestrator = FullDuplexOrchestrator(
        "mock",
        agent,
        Caller(),
        environment,
        task,
        tick_duration_seconds=0.005,
        max_steps=200,
    )
    try:
        orchestrator.initialize()
        assert (
            not runtime.started
        )  # Prepared input is not delivered until the agent tick.
        while not orchestrator.done and orchestrator.step_count < 200:
            orchestrator.step()
            orchestrator._check_termination()
        assert agent.calls > 3
        assert agent.received is not None
        assert [r.name for r in runtime.records if r.kind == "environment"] == [
            "change"
        ]
        assert len([r for r in runtime.records if r.kind == "tool"]) == 1
    finally:
        runtime.close()
        orchestrator._tool_executor.close()


def test_grounding_rejects_an_invented_user_preference(monkeypatch):
    from tau2.user.grounding import validate_user_candidate
    from tau2.user.user_simulator_base import UserError

    monkeypatch.setattr(
        "tau2.user.grounding.generate",
        lambda **kw: AssistantMessage(
            role="assistant",
            content='{"valid":false,"reason":"Changed the requested appointment date"}',
            cost=0,
        ),
    )
    with pytest.raises(UserError, match="grounding"):
        validate_user_candidate(
            allowed="Book October 21",
            conversation="agent: Shall I use October 22?",
            candidate="Yes, October 22 instead",
            model="unused",
            model_args={},
        )

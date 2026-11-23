"""Playback authorization races, with deterministic futures and no API calls."""

from concurrent.futures import Future

import pytest

from tau2.data_model.audio import AudioEncoding, AudioFormat, audio_bytes_to_string
from tau2.data_model.message import (
    AssistantMessage,
    ToolCall,
    ToolMessage,
    TurnTakingAction,
    UserMessage,
)
from tau2.data_model.voice import SynthesisConfig, VoiceSettings
from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator


class ControlledExecutor:
    """Make requests look running until the test explicitly finishes them."""

    def __init__(self):
        self.calls = []
        self.mark_running = True

    def submit(self, function, *args):
        future = Future()
        if self.mark_running:
            future.set_running_or_notify_cancel()
        self.calls.append((future, function, args))
        return future

    def finish(self, message=None, *, index=-1, error=None):
        future, function, args = self.calls[index]
        if error is not None:
            future.set_exception(error)
        elif message is None:
            future.set_result(function(*args))
        elif isinstance(message, TurnTakingAction):
            future.set_result(message)
        else:
            snapshot = args[-1]
            snapshot.user_utterance_count += int(message.has_audio_content())
            snapshot._llm_generation_seconds = 0.1
            snapshot._tts_synthesis_seconds = (
                0.2 if message.has_audio_content() else None
            )
            future.set_result((message, snapshot))

    def shutdown(self, **kwargs):
        pass


def utterance(text="old question", cost=0.3):
    return UserMessage(
        role="user",
        content=text,
        cost=cost,
        usage={"prompt_tokens": 10},
        is_audio=True,
        contains_speech=True,
        audio_content=audio_bytes_to_string(b"\x01\x00" * 3200),
        audio_script_gold=text,
        audio_format=AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000),
    )


def agent(text="", speech=False, event_id=None):
    return AssistantMessage(
        role="assistant",
        content=text,
        contains_speech=speech,
        raw_data={"live_transcript": {"fragments": [{"event_id": event_id}]}}
        if event_id
        else None,
    )


def test_unchanged_streaming_snapshot_does_not_invent_new_heard_text(caller):
    user, state, _ = caller

    def chunk(fragments):
        return AssistantMessage(
            role="assistant",
            content="Already confirmed words",
            contains_speech=False,
            raw_data={
                "live_transcript": {
                    "snapshot": {"version": 1, "text": "Already confirmed words"},
                    "fragments": fragments,
                }
            },
        )

    user._observe_agent_input(state, chunk([]))
    assert state.agent_input_revision == 0
    fresh = chunk([{"event_id": "confirmed-1", "text": "Already confirmed words"}])
    user._observe_agent_input(state, fresh)
    assert state.agent_input_revision == 1
    user._observe_agent_input(state, fresh)
    user._observe_agent_input(state, chunk([]))
    assert state.agent_input_revision == 1


@pytest.fixture
def caller(monkeypatch):
    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Ask for help in the mock domain.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        chunk_size=1600,
        tick_duration_seconds=0.2,
        integration_ticks=2,
        realtime_generation=True,
        use_llm_backchannel=False,
    )
    user._user_executor.shutdown()
    executor = ControlledExecutor()
    user._user_executor = executor
    user.enable_user_initiated_interruption = True
    state = user.get_init_state()
    user._latest_state = state
    monkeypatch.setattr(
        user, "_compute_turn_taking_action", lambda _: TurnTakingAction(action="wait")
    )

    def effects(chunk, state, is_speech):
        state.elapsed_samples += user.chunk_size
        return chunk or UserMessage(role="user", content="", contains_speech=False)

    monkeypatch.setattr(user, "_apply_chunk_effects", effects)
    try:
        yield user, state, executor
    finally:
        for i, (future, _, args) in enumerate(executor.calls):
            if not future.done():
                if args[0].__name__ == "<lambda>":
                    executor.finish(TurnTakingAction(action="wait"), index=i)
                else:
                    executor.finish(
                        UserMessage(role="user", content="", cost=0), index=i
                    )
        user.stop(state=state)


def start(user, state, origin="normal", action="generate_message"):
    return user._perform_turn_taking_action(
        state, TurnTakingAction(action=action, generation_origin=origin)
    )[0]


@pytest.mark.parametrize("silent_ticks", [26, 65, 149, 150, 200])
def test_silence_checkins_wait_for_cooldown(caller, monkeypatch, silent_ticks):
    """A silent agent gets at least 30 seconds after the caller finishes."""
    user, state, _ = caller
    monkeypatch.setattr(
        user,
        "_compute_turn_taking_action",
        VoiceStreamingUserSimulator._compute_turn_taking_action.__get__(user),
    )
    user.silence_checkin_threshold = 150
    state.user_utterance_count = 1
    state.tick_count = 1000
    state.time_since_last_talk = silent_ticks
    state.time_since_last_other_talk = 1000
    action = user._next_turn_taking_action(state)
    assert action.action == ("wait" if silent_ticks < 150 else "generate_message")


@pytest.mark.parametrize("speech", [False, True])
def test_fresh_agent_input_bypasses_silence_cooldown(caller, monkeypatch, speech):
    """Questions and approvals retain the ordinary response delay."""
    user, state, _ = caller
    monkeypatch.setattr(
        user,
        "_compute_turn_taking_action",
        VoiceStreamingUserSimulator._compute_turn_taking_action.__get__(user),
    )
    user.silence_checkin_threshold = 150
    state.user_utterance_count = 1
    state.tick_count = 1000
    state.time_since_last_talk = 26
    state.time_since_last_other_talk = 6
    state.input_turn_taking_buffer = [agent("May I proceed?", speech), agent()]
    assert user._next_turn_taking_action(state).action == "generate_message"


def test_initial_request_and_disabled_cooldown_keep_original_timing(caller):
    user, state, _ = caller
    user.silence_checkin_threshold = 150
    state.tick_count = 26
    state.time_since_last_talk = state.time_since_last_other_talk = 26
    decide = VoiceStreamingUserSimulator._compute_turn_taking_action
    assert decide(user, state).action == "generate_message"
    state.user_utterance_count = 1
    assert decide(user, state).action == "wait"
    user.silence_checkin_threshold = 0
    assert decide(user, state).action == "generate_message"


@pytest.mark.parametrize(
    "result",
    [
        utterance(),
        UserMessage(role="user", content="###STOP###", cost=0.3),
        UserMessage(
            role="user",
            tool_calls=[
                ToolCall(
                    id="never-execute",
                    name="mock_action",
                    arguments={},
                    requestor="user",
                )
            ],
            cost=0.3,
        ),
    ],
)
def test_reply_arriving_during_preparation_invalidates_all_kinds_of_output(
    caller, result
):
    user, state, executor = caller
    assert start(user, state).contains_speech is False
    for _ in range(27):
        user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent("New answer", True))
    executor.finish(result)
    chunk, state = user.get_next_chunk(state, agent("continues", True))
    assert not chunk.contains_speech and not chunk.is_tool_call()
    assert "###STOP###" not in (chunk.content or "")
    assert state.user_utterance_count == 0 and not state.output_streaming_queue
    assert "New answer" in "".join(
        c.content or "" for c in state.input_turn_taking_buffer
    )
    assert user.get_generation_diagnostics()["additional_llm_cost"] == 0.3


@pytest.mark.parametrize(
    "new_input", [agent("delayed transcript"), agent("voice", True)]
)
def test_obsolete_result_stays_obsolete_after_agent_finishes(caller, new_input):
    user, state, executor = caller
    start(user, state)
    user.get_next_chunk(state, new_input)
    for _ in range(3):
        user.get_next_chunk(state, agent())
    executor.finish(utterance())
    chunk, state = user.get_next_chunk(state, agent())
    assert not chunk.contains_speech
    assert user._generation is None and state.user_utterance_count == 0


@pytest.mark.parametrize(
    "result",
    [
        utterance("fresh reply"),
        UserMessage(role="user", content="###STOP###", cost=0.1),
    ],
)
def test_silence_does_not_invalidate_an_unchanged_candidate(caller, result):
    user, state, executor = caller
    start(user, state)
    for _ in range(4):
        user.get_next_chunk(state, agent())
    executor.finish(result)
    chunk, state = user.get_next_chunk(state, agent())
    assert chunk.contains_speech or chunk.content == "###STOP###"
    events = user.get_generation_diagnostics()["events"]
    assert len([event for event in events if event["event"] == "committed"]) == 1
    assert user.get_generation_diagnostics()["additional_llm_cost"] == 0


def test_repeated_asr_fragment_does_not_invalidate_twice(caller):
    user, state, executor = caller
    user._observe_agent_input(state, agent("yes", event_id="segment-1"))
    start(user, state)
    user.get_next_chunk(state, agent("yes", event_id="segment-1"))
    executor.finish(utterance())
    chunk, state = user.get_next_chunk(state, agent())
    assert chunk.contains_speech


def test_updated_text_with_the_same_event_id_is_new_input(caller):
    user, state, executor = caller
    user._observe_agent_input(state, agent("partial", event_id="segment-1"))
    start(user, state)
    user.get_next_chunk(state, agent("completed answer", event_id="segment-1"))
    executor.finish(utterance())
    assert not user.get_next_chunk(state, agent())[0].contains_speech


def test_stop_saves_audio_even_when_an_unplayed_request_failed(caller):
    from types import SimpleNamespace

    user, state, executor = caller
    saved = []
    user._audio_taps = {"fixture": SimpleNamespace(save=lambda: saved.append(True))}
    start(user, state)
    executor.finish(error=RuntimeError("late failure"))
    with pytest.raises(RuntimeError, match="late failure"):
        user.stop(state=state)
    assert saved == [True]


@pytest.mark.parametrize(
    "origin,action", [("interrupt", "generate_message"), ("backchannel", "backchannel")]
)
def test_authorized_reaction_can_play_in_the_same_speech_episode(
    caller, origin, action
):
    user, state, executor = caller
    user._observe_agent_input(state, agent("first words", True))
    start(user, state, origin, action)
    user.get_next_chunk(state, agent("more words", True))
    executor.finish(utterance("yes", cost=0))
    chunk, state = user.get_next_chunk(state, agent("still speaking", True))
    assert chunk.contains_speech
    assert state.user_utterance_count == 1
    committed = [
        e
        for e in user.get_generation_diagnostics()["events"]
        if e["event"] == "committed"
    ]
    assert committed[0]["origin"] == origin and committed[0]["agent_speaking"]


@pytest.mark.parametrize(
    "origin,action", [("interrupt", "generate_message"), ("backchannel", "backchannel")]
)
def test_reaction_expires_when_its_speech_episode_ends(caller, origin, action):
    user, state, executor = caller
    user._observe_agent_input(state, agent("first", True))
    start(user, state, origin, action)
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent("new episode", True))
    executor.finish(utterance())
    chunk, state = user.get_next_chunk(state, agent("new", True))
    assert not chunk.contains_speech and state.user_utterance_count == 0


def test_stale_listener_is_reaped_without_being_applied(caller):
    user, state, executor = caller
    user.get_next_chunk(state, agent("first", True))
    assert len(executor.calls) == 1
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent())
    executor.finish(
        TurnTakingAction(
            action="generate_message",
            generation_origin="interrupt",
            interrupt_check_cost=0.1,
        )
    )
    chunk, state = user.get_next_chunk(state, agent())
    assert not chunk.contains_speech and user._generation is None
    assert user.get_generation_diagnostics()["additional_llm_cost"] == 0.1


def test_current_listener_authorization_is_preserved(caller):
    user, state, executor = caller
    user.get_next_chunk(state, agent("first", True))
    executor.finish(
        TurnTakingAction(
            action="generate_message",
            generation_origin="interrupt",
            interrupt_check_cost=0.1,
        )
    )
    user.get_next_chunk(state, agent("continued", True))
    assert len(executor.calls) == 2
    executor.finish(utterance("intentional interruption"))
    chunk, state = user.get_next_chunk(state, agent("still", True))
    assert chunk.contains_speech
    assert chunk.turn_taking_action.interrupt_check_cost == 0.1


def test_direct_delivery_cannot_bypass_the_last_moment_check(caller):
    user, state, executor = caller
    start(user, state)
    executor.finish(utterance())
    user._observe_agent_input(state, agent("new information", True))
    chunk = start(user, state)
    assert not chunk.contains_speech and state.user_utterance_count == 0


def test_cancelled_requests_do_not_advance_spelling_or_utterance_state(
    caller, monkeypatch
):
    user, state, executor = caller
    detections = []
    user.spell_event_detector = lambda text: detections.append(text) or []
    start(user, state)
    user.get_next_chunk(state, agent("new", True))
    executor.finish(utterance())
    user.get_next_chunk(state, agent())
    assert detections == [] and state.user_utterance_count == 0
    assert not user.get_effect_timeline().events


def test_tool_result_survives_discarded_speech_and_is_not_reexecuted(caller):
    user, state, executor = caller
    result = ToolMessage(
        id="executed-once", role="tool", content="done", requestor="user"
    )
    user.get_next_chunk(state, agent(), tool_results=result)
    user.get_next_chunk(state, agent("new answer", True))
    executor.finish(utterance())
    user.get_next_chunk(state, agent())
    assert user._needs_tool_reply
    assert any(t.env_chunk == result for t in state.ticks)
    assert not any(t.self_chunk and t.self_chunk.is_tool_call() for t in state.ticks)
    user.get_next_chunk(state, agent())
    start(user, state)
    executor.finish(utterance("report result"))
    chunk = start(user, state)
    assert chunk.contains_speech and not user._needs_tool_reply
    assert chunk.turn_taking_action.generation_origin == "tool_result"


@pytest.mark.parametrize("cost", [None, 0.5])
def test_discarded_work_is_accounted_once_and_unknown_cost_stays_unknown(caller, cost):
    user, state, executor = caller
    start(user, state)
    user.get_next_chunk(state, agent("new answer", True))
    executor.finish(utterance(cost=cost))
    for _ in range(3):
        user.get_next_chunk(state, agent())
    diagnostics = user.get_generation_diagnostics()
    assert diagnostics["additional_llm_cost"] == cost
    assert len([e for e in diagnostics["events"] if e["event"] == "settled"]) == 1


def test_stale_request_exception_is_not_hidden(caller):
    user, state, executor = caller
    start(user, state)
    user.get_next_chunk(state, agent("new", True))
    executor.finish(error=RuntimeError("request failed"))
    with pytest.raises(RuntimeError, match="request failed"):
        user.get_next_chunk(state, agent())


def test_no_replacement_queue_accumulates_while_an_old_request_is_running(caller):
    user, state, executor = caller
    start(user, state)
    for _ in range(30):
        chunk, state = user.get_next_chunk(state, agent("new speech", True))
        assert not chunk.contains_speech
    assert len(executor.calls) == 1 and state.tick_count == 30


def test_replacement_uses_latest_input_instead_of_delaying_the_old_question(caller):
    user, state, executor = caller
    start(user, state)
    user.get_next_chunk(state, agent("The requested work is complete", True))
    executor.finish(utterance("Is the work complete?"))
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent())
    start(user, state)
    snapshot = executor.calls[-1][2][-1]
    assert "The requested work is complete" in "".join(
        c.content or "" for c in snapshot.input_turn_taking_buffer
    )
    executor.finish(utterance("Thank you"))
    reply = start(user, state)
    spoken = (reply.content or "") + "".join(
        chunk.content or "" for chunk in state.output_streaming_queue
    )
    assert spoken == "Thank you" and state.user_utterance_count == 1


def test_not_yet_started_request_is_cancelled_without_charging_an_api_call(caller):
    user, state, executor = caller
    executor.mark_running = False
    start(user, state)
    user.get_next_chunk(state, agent("new", True))
    assert executor.calls[0][0].cancelled()
    assert user._generation is None
    assert user.get_generation_diagnostics()["additional_llm_cost"] == 0


def test_listener_failure_is_not_hidden_after_its_decision_expires(caller):
    user, state, executor = caller
    user.get_next_chunk(state, agent("first", True))
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent())
    executor.finish(error=RuntimeError("listener failed"))
    with pytest.raises(RuntimeError, match="listener failed"):
        user.get_next_chunk(state, agent())


def test_a_short_speech_pause_preserves_a_deliberate_interruption(caller):
    user, state, executor = caller
    user._observe_agent_input(state, agent("first", True))
    start(user, state, "interrupt")
    user.get_next_chunk(state, agent())
    user.get_next_chunk(state, agent("continued", True))
    executor.finish(utterance())
    assert user.get_next_chunk(state, agent("continued", True))[0].contains_speech


@pytest.mark.parametrize("new_reply", [False, True])
def test_background_synthesis_never_commits_spelling_events(
    caller, monkeypatch, new_reply
):
    user, state, executor = caller
    detections = []
    user.spell_event_detector = lambda text: detections.append(text) or []
    monkeypatch.setattr(
        "tau2.user.user_simulator_streaming.generate",
        lambda **_: AssistantMessage(
            role="assistant", content="spell A B", cost=0.2, generation_time_seconds=0.1
        ),
    )
    monkeypatch.setattr(
        user,
        "synthesize_voice",
        lambda message, state, **_: utterance(message.content, 0.2),
    )
    start(user, state)
    executor.finish()
    assert detections == [] and state.user_utterance_count == 0
    if new_reply:
        user._observe_agent_input(state, agent("new reply", True))
    result = start(user, state)
    assert bool(detections) == (not new_reply)
    assert result.contains_speech == (not new_reply)


@pytest.mark.parametrize("additional, expected", [(0.3, 0.5), (None, None)])
def test_final_simulation_includes_unplayed_cost_and_diagnostics(additional, expected):
    from time import perf_counter
    from types import SimpleNamespace

    from tau2.data_model.simulation import TerminationReason
    from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
    from tau2.orchestrator.modes import CommunicationMode

    diagnostics = {"version": "v1.2", "additional_llm_cost": additional, "events": []}
    shutdown_order = []
    run = SimpleNamespace(
        agent=SimpleNamespace(stop=lambda *a, **kw: shutdown_order.append("agent")),
        user=SimpleNamespace(
            stop=lambda *a, **kw: shutdown_order.append("user"),
            cancel_pending_generation=lambda: shutdown_order.append("cancel"),
            get_generation_diagnostics=lambda: diagnostics,
        ),
        agent_state=None,
        user_state=None,
        pending_agent_tool_results=[],
        pending_user_tool_results=[],
        _run_start_perf=perf_counter(),
        _run_start_time="2026-01-01T00:00:00",
        get_trajectory=lambda: [],
        get_messages=lambda: [UserMessage(role="user", content="spoken", cost=0.2)],
        simulation_id="fixture",
        task=SimpleNamespace(id="mock"),
        termination_reason=TerminationReason.USER_STOP,
        seed=1,
        mode=CommunicationMode.FULL_DUPLEX,
    )
    result = FullDuplexOrchestrator._finalize(run)
    assert result.user_cost == expected
    assert result.info["user_generation"] == diagnostics
    assert shutdown_order == ["cancel", "agent", "user"]

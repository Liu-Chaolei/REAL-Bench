"""
Tests for basic_turn_taking_policy.

Focuses on the delivering_tool_result_speech threshold override
and overlap/interruption logic.
"""

import pytest

from tau2.agent.base.streaming import (
    StreamingState,
    basic_turn_taking_policy,
)
from tau2.data_model.message import AssistantMessage, UserMessage


def _make_state(
    output_queue_size: int = 0,
    input_buffer_speech_ticks: int = 0,
    consecutive_self_speaking: int = 0,
    consecutive_other_speaking: int = 0,
    time_since_last_talk: int = 0,
    time_since_last_other_talk: int = 0,
    delivering_tool_result_speech: bool = False,
) -> StreamingState:
    """Build a StreamingState with the given overlap/timing configuration."""
    output_queue = [
        AssistantMessage(role="assistant", content=f"chunk_{i}", contains_speech=True)
        for i in range(output_queue_size)
    ]

    input_buffer = [
        UserMessage(role="user", content=f"speech_{i}", contains_speech=True)
        for i in range(input_buffer_speech_ticks)
    ]

    return StreamingState(
        output_streaming_queue=output_queue,
        input_turn_taking_buffer=input_buffer,
        consecutive_self_speaking_ticks=consecutive_self_speaking,
        consecutive_other_speaking_ticks=consecutive_other_speaking,
        time_since_last_talk=time_since_last_talk,
        time_since_last_other_talk=time_since_last_other_talk,
        delivering_tool_result_speech=delivering_tool_result_speech,
    )


class TestDeliveringToolResultSpeech:
    """Tests for the delivering_tool_result_speech threshold override."""

    def test_tool_result_speech_uses_interrupting_threshold(self):
        """When delivering tool result speech, the high 'interrupting' threshold
        should be used regardless of who initiated the overlap.

        This is the test that would have caught the overlap_initiator scoping bug.
        """
        state = _make_state(
            output_queue_size=5,
            input_buffer_speech_ticks=3,
            consecutive_self_speaking=10,
            consecutive_other_speaking=3,
            delivering_tool_result_speech=True,
        )

        # With the high threshold (10), 3 ticks of interruption is not enough
        action, info = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=2,
            yield_threshold_when_interrupting=10,
        )
        assert action == "keep_talking"

    def test_tool_result_speech_still_yields_past_threshold(self):
        """Tool result speech is harder to interrupt but not immune.
        Past the high threshold, the participant should yield."""
        state = _make_state(
            output_queue_size=5,
            input_buffer_speech_ticks=15,
            consecutive_self_speaking=20,
            consecutive_other_speaking=15,
            delivering_tool_result_speech=True,
        )

        action, info = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=2,
            yield_threshold_when_interrupting=10,
        )
        assert action == "stop_talking"

    def test_normal_speech_uses_interrupted_threshold_when_other_initiated(self):
        """Without delivering_tool_result_speech, when other initiated the overlap,
        the lower 'interrupted' threshold is used."""
        # Other has been speaking longer -> other initiated the overlap
        # (self was speaking first, other barged in)
        state = _make_state(
            output_queue_size=5,
            input_buffer_speech_ticks=3,
            consecutive_self_speaking=10,
            consecutive_other_speaking=3,
        )

        action, info = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=2,
            yield_threshold_when_interrupting=10,
        )
        # 3 ticks > threshold of 2 -> should stop
        assert action == "stop_talking"

    def test_normal_speech_uses_interrupting_threshold_when_self_initiated(self):
        """Without delivering_tool_result_speech, when self initiated the overlap,
        the higher 'interrupting' threshold is used."""
        # Self has been speaking shorter -> self initiated the overlap
        # (other was speaking first, self barged in)
        state = _make_state(
            output_queue_size=5,
            input_buffer_speech_ticks=3,
            consecutive_self_speaking=3,
            consecutive_other_speaking=10,
        )

        action, info = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=2,
            yield_threshold_when_interrupting=10,
        )
        # 3 ticks < threshold of 10 -> should keep talking
        assert action == "keep_talking"


class TestBasicTurnTakingActions:
    """Smoke tests for basic turn-taking action selection."""

    def test_wait_when_not_talking_and_below_threshold(self):
        """When not talking and other hasn't spoken long enough, wait."""
        state = _make_state(
            time_since_last_other_talk=0,
            time_since_last_talk=0,
        )

        action, _ = basic_turn_taking_policy(
            state,
            wait_to_respond_threshold_other=5,
            wait_to_respond_threshold_self=5,
        )
        assert action == "wait"

    def test_generate_when_thresholds_met(self):
        """When both wait thresholds are met, generate a message."""
        state = _make_state(
            input_buffer_speech_ticks=3,
            time_since_last_other_talk=10,
            time_since_last_talk=10,
        )

        action, _ = basic_turn_taking_policy(
            state,
            wait_to_respond_threshold_other=2,
            wait_to_respond_threshold_self=2,
        )
        assert action == "generate_message"

    def test_keep_talking_when_no_interruption(self):
        """When talking and other is silent, keep talking."""
        state = _make_state(
            output_queue_size=3,
            consecutive_self_speaking=5,
            consecutive_other_speaking=0,
        )

        action, _ = basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=2,
        )
        assert action == "keep_talking"


class TestCurrentSpeechTurnTaking:
    """Current speech drives decisions; historical speech stays in evidence."""

    @pytest.mark.parametrize("speech_ticks", [5, 7, 9])
    def test_0760_speech_ending_does_not_stop_active_user(self, speech_ticks):
        state = _make_state(
            output_queue_size=3,
            input_buffer_speech_ticks=speech_ticks,
            consecutive_self_speaking=speech_ticks,
            consecutive_other_speaking=0,
        )
        state.input_turn_taking_buffer[-1].raw_data = {"speaking_now": False}
        before = state.model_dump()

        assert state.input_ongoing_speech_duration() == 0
        assert not state.input_interrupt()
        assert state.input_total_speech_duration() == speech_ticks
        assert (
            basic_turn_taking_policy(
                state,
                yield_threshold_when_interrupted=5,
                yield_threshold_when_interrupting=25,
            )[0]
            == "keep_talking"
        )
        assert state.model_dump() == before

    @pytest.mark.parametrize("delivering_result", [False, True])
    def test_no_overlap_is_not_treated_as_other_initiated(self, delivering_result):
        state = _make_state(
            output_queue_size=3,
            input_buffer_speech_ticks=30,
            consecutive_self_speaking=0,
            consecutive_other_speaking=30,
            delivering_tool_result_speech=delivering_result,
        )
        assert state.input_interrupt() and state.overlap_initiator is None
        assert basic_turn_taking_policy(
            state,
            yield_threshold_when_interrupted=5,
            yield_threshold_when_interrupting=25,
        ) == ("keep_talking", "No current overlap")

    @pytest.mark.parametrize(
        "speech_ticks,expected", [(5, "keep_talking"), (25, "stop_talking")]
    )
    def test_active_self_overlap_retains_high_threshold(self, speech_ticks, expected):
        state = _make_state(
            output_queue_size=3,
            input_buffer_speech_ticks=speech_ticks,
            consecutive_self_speaking=speech_ticks,
            consecutive_other_speaking=speech_ticks + 10,
        )
        for chunk in state.input_turn_taking_buffer:
            chunk.raw_data = {"speaking_now": True}
        assert state.overlap_initiator == "self"
        assert (
            basic_turn_taking_policy(
                state,
                yield_threshold_when_interrupted=5,
                yield_threshold_when_interrupting=25,
            )[0]
            == expected
        )

    def test_pause_then_new_other_overlap_restarts_duration(self):
        state = _make_state(
            output_queue_size=3,
            input_buffer_speech_ticks=9,
            consecutive_self_speaking=9,
            consecutive_other_speaking=0,
        )
        state.input_turn_taking_buffer[-1].raw_data = {"speaking_now": False}
        thresholds = dict(
            yield_threshold_when_interrupted=5, yield_threshold_when_interrupting=25
        )
        assert basic_turn_taking_policy(state, **thresholds)[0] == "keep_talking"
        for count in range(1, 6):
            state.update_input_turn_taking_buffer(
                UserMessage(
                    role="user", contains_speech=True, raw_data={"speaking_now": True}
                )
            )
            state.consecutive_self_speaking_ticks += 1
            state.consecutive_other_speaking_ticks = count
            assert state.input_ongoing_speech_duration() == count
            assert state.overlap_initiator == "other"
            assert basic_turn_taking_policy(state, **thresholds)[0] == (
                "stop_talking" if count == 5 else "keep_talking"
            )

    @pytest.mark.parametrize("contains_speech", [False, True])
    def test_legacy_messages_without_current_state_keep_their_semantics(
        self, contains_speech
    ):
        state = _make_state(output_queue_size=3)
        state.update_input_turn_taking_buffer(
            UserMessage(
                role="user", content="Legacy reply", contains_speech=contains_speech
            )
        )
        assert state.input_ongoing_speech_duration() == int(contains_speech)
        assert state.input_total_speech_duration() == int(contains_speech)

    def test_backchannel_still_takes_priority_over_overlap(self):
        state = _make_state(
            output_queue_size=2,
            input_buffer_speech_ticks=10,
            consecutive_self_speaking=10,
            consecutive_other_speaking=5,
        )
        state.is_backchanneling = True
        assert basic_turn_taking_policy(state, yield_threshold_when_interrupted=5) == (
            "keep_talking",
            "Backchannel in progress",
        )

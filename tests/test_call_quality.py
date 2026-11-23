"""Retained selectivity and tick measurements on synthetic calls."""

import pytest

from tau2.data_model.message import AssistantMessage, Tick, UserMessage
from tau2.data_model.simulation import SimulationRun
from tau2.metrics.interaction_quality import (
    InteractionMeasurements,
    measure_interaction,
)
from tau2.scripts.leaderboard.benchmark_quality import (
    cell_quality,
    score_call,
)


def _simulation(ticks=None) -> SimulationRun:
    return SimulationRun(
        id="sim",
        task_id="task",
        trial=0,
        start_time="a",
        end_time="b",
        duration=1,
        termination_reason="user_stop",
        ticks=ticks,
    )


def _timeline(user, agent, length, silent=()) -> SimulationRun:
    """Caller and agent speech spans (inclusive tick ranges), 0.2 s per tick.

    ``silent`` agent spans carry chunks without speech (``contains_speech``
    false), as providers emit around tool calls.
    """

    def owner(spans, tick):
        return next((n for n, (a, b) in enumerate(spans) if a <= tick <= b), None)

    ticks = []
    for tick in range(length):
        caller, speaker, quiet = (
            owner(user, tick),
            owner(agent, tick),
            owner(silent, tick),
        )
        agent_chunk = None
        if speaker is not None:
            agent_chunk = AssistantMessage(
                role="assistant", content=f"a{tick} ", utterance_ids=[f"a{speaker}"]
            )
        elif quiet is not None:
            agent_chunk = AssistantMessage(
                role="assistant",
                content="",
                contains_speech=False,
                utterance_ids=[f"q{quiet}"],
            )
        ticks.append(
            Tick(
                tick_id=tick,
                timestamp=str(tick),
                tick_duration_seconds=0.2,
                user_chunk=UserMessage(
                    role="user", content=f"u{tick} ", utterance_ids=[f"u{caller}"]
                )
                if caller is not None
                else None,
                agent_chunk=agent_chunk,
            )
        )
    return _simulation(ticks)


def _measured(**overrides) -> InteractionMeasurements:
    values = dict(
        response_total=2,
        responses=2,
        response_latency_mean=1.0,
        yield_total=0,
        yields=0,
        backchannel_total=0,
        backchannel_errors=0,
        vocal_tic_total=0,
        vocal_tic_errors=0,
        non_directed_total=0,
        non_directed_errors=0,
        agent_interruptions=0,
        max_agent_floor_hold_seconds=10.0,
    )
    return InteractionMeasurements(**(values | overrides))


def test_measurements_come_from_the_tick_timeline():
    assert measure_interaction(_simulation()) is None
    answered = measure_interaction(_timeline([(0, 9)], [(12, 20)], 40))
    assert (answered.response_total, answered.responses) == (1, 1)
    assert answered.agent_interruptions == 0
    assert answered.max_agent_floor_hold_seconds == pytest.approx(1.8)
    # Four agent onsets over one long caller turn.
    talked_over = measure_interaction(
        _timeline([(0, 30)], [(5, 8), (12, 14), (18, 20), (24, 26)], 60)
    )
    assert talked_over.agent_interruptions == 4
    # Agent pauses between caller turns still hold the floor.
    held = measure_interaction(_timeline([(0, 4), (40, 44)], [(6, 10), (20, 34)], 50))
    assert held.max_agent_floor_hold_seconds == pytest.approx(29 * 0.2)


def test_retained_flags_have_no_combined_or_generation_score():
    call = score_call(_simulation())
    assert "flags" not in call.model_dump()
    cell = cell_quality("en", "gaia2", [call])
    assert "metrics" not in cell.model_dump()
    assert "experience" not in cell.model_dump()
    assert "interaction" not in cell.model_dump()

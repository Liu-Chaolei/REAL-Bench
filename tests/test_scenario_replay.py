"""Replay preserves notification delivery and delayed causal bindings."""

import itertools

from tau2.data_model.message import ToolCall
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.environment.db import DB
from tau2.environment.environment import Environment
from tau2.environment.replay import replay_environment
from tau2.environment.scenario import ScenarioRuntime
from tau2.environment.toolkit import ToolKitBase, is_tool


class _Tools(ToolKitBase):
    @is_tool()
    def parent(self) -> str:
        """Return the parent operation's actual identifier."""
        return "parent-result"

    @is_tool()
    def child(self) -> str:
        """Return an effect observed before its causal parent."""
        return "child-result"

    @is_tool()
    def get_notifications(self) -> list[dict]:
        """Consume currently delivered notifications."""
        with self.runtime.lock:
            result, self.runtime.notifications = self.runtime.notifications, []
            return result

    @is_tool()
    def wait(self, timeout: int = 0) -> list[dict]:
        """Wait for notifications while leaving the scheduler unblocked."""
        return self.runtime.wait_for_notification(timeout)

    wait.__waits_for_environment__ = True


def _environment(events):
    tools = _Tools(DB())
    environment = Environment("mock", "Replay fixture", tools=tools)
    runtime = ScenarioRuntime(
        ScenarioSpec(
            start_time=1000,
            source_id="replay",
            source_sha256="0" * 64,
            snapshot="1" * 64,
            category="test",
            events=events,
        ),
        lambda name, args: args["value"],
    )
    environment.scenario_runtime = tools.runtime = runtime
    return environment


def _call(environment, name, identifier):
    return environment.get_response(
        ToolCall(id=identifier, name=name, arguments={}, requestor="assistant")
    )


def test_replay_restores_notifications_and_only_consumes_the_recorded_wait_batch():
    events = [
        ScenarioAction(
            id=str(i),
            kind="environment",
            tool="arrival",
            arguments={"value": i},
            delay_seconds=i,
        )
        for i in (1, 2)
    ]
    environment = _environment(events)
    runtime = environment.scenario_runtime
    runtime.advance(1)
    consumed = environment.tools.wait()
    # A second event arrives after the wait returns but before it is journaled.
    runtime.advance(2)
    runtime.record(
        "tool",
        name="wait",
        arguments={"timeout": 0},
        result=consumed,
        call_id="wait",
        started_time=1000.5,
    )
    assert not _call(environment, "get_notifications", "poll").error
    replayed = _environment(events)
    try:
        replay_environment(replayed, runtime.records)
        assert replayed.scenario_runtime.notifications == []
    finally:
        environment.close()
        replayed.close()


def test_replay_matches_the_tool_record_when_an_earlier_effect_binds_late():
    events = [
        ScenarioAction(id="u", kind="user", tool="input", arguments={"content": "go"}),
        ScenarioAction(id="p", kind="reference", tool="parent", dependencies=["u"]),
        ScenarioAction(id="c", kind="reference", tool="child", dependencies=["p"]),
    ]
    environment = _environment(events)
    runtime = environment.scenario_runtime
    runtime.delivered("user", "go", event_id="u")
    _call(environment, "child", "early")
    runtime.advance(1)
    _call(environment, "parent", "later")
    assert runtime.records[-1].result == "child-result"
    replayed = _environment(events)
    try:
        replay_environment(replayed, runtime.records)
        assert replayed.scenario_runtime.results["p"] == "parent-result"
        assert replayed.scenario_runtime.results["c"] == "child-result"
    finally:
        environment.close()
        replayed.close()


def test_real_clock_notifications_use_the_exact_journal_timestamp():
    environment = _environment(
        [
            ScenarioAction(
                id="arrival", kind="environment", tool="arrival", arguments={"value": 1}
            )
        ]
    )
    runtime = environment.scenario_runtime
    runtime.monotonic = lambda: next(ticks)
    ticks = itertools.count(0, 0.001)
    runtime._origin = 0
    runtime._manual = None
    try:
        runtime.advance()
        assert runtime.notifications[0]["time"] == runtime.records[0].time
    finally:
        environment.close()


def test_wait_replay_accepts_exact_string_timestamp_but_not_missing_arrival():
    import pytest

    environment = _environment(
        [
            ScenarioAction(
                id="arrival",
                kind="environment",
                tool="arrival",
                arguments={"value": "id"},
                delay_seconds=1,
            )
        ]
    )
    runtime = environment.scenario_runtime
    runtime.advance(1)
    notifications = environment.tools.wait()
    notifications[0]["time"] = str(notifications[0]["time"])
    runtime.record(
        "tool",
        name="wait",
        arguments={"timeout": 0},
        result=notifications,
        call_id="wait",
    )
    replayed = _environment(runtime.spec.events)
    try:
        replay_environment(replayed, runtime.records)
        assert replayed.scenario_runtime.notifications == []
        broken = [r.model_dump(mode="json") for r in runtime.records]
        broken[-1]["result"][0]["time"] = "9999"
        with pytest.raises(ValueError, match="undelivered"):
            replay_environment(_environment(runtime.spec.events), broken)
    finally:
        environment.close()
        replayed.close()


def test_tool_result_mismatch_can_be_recorded_without_blocking_replay():
    environment = _environment([])
    _call(environment, "parent", "original")
    records = [
        record.model_dump(mode="json")
        for record in environment.scenario_runtime.records
    ]
    records[-1]["result"] = "stale-result"
    replayed = _environment([])
    warnings = []
    try:
        replay_environment(
            replayed,
            records,
            strict=True,
            compare_tool_results=False,
            replay_warnings=warnings,
        )
        assert warnings == [{"tool": "parent"}]
    finally:
        environment.close()
        replayed.close()


def test_tool_error_still_blocks_when_result_comparison_is_disabled():
    environment = _environment([])
    _call(environment, "parent", "original")
    records = [
        record.model_dump(mode="json")
        for record in environment.scenario_runtime.records
    ]
    records[-1]["error"] = True
    replayed = _environment([])
    try:
        import pytest

        with pytest.raises(ValueError, match="Tool replay differs"):
            replay_environment(replayed, records, compare_tool_results=False)
    finally:
        environment.close()
        replayed.close()

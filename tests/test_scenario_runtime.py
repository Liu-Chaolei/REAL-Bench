"""Timed environments share one clock, execution queue and replay protocol."""

import threading
import time
from concurrent.futures import Future

import pytest

from tau2.data_model.message import Tick, ToolCall, ToolMessage
from tau2.data_model.scenario import ScenarioAction, ScenarioSpec
from tau2.environment.execution import ToolExecutionQueue
from tau2.environment.scenario import ScenarioRuntime, resolve_references
from tau2.utils.transcript.views import tool_messages


def spec(events):
    return ScenarioSpec(
        start_time=1000,
        source_id="fixture",
        source_sha256="0" * 64,
        snapshot="1" * 64,
        category="test",
        events=events,
    )


def test_causal_event_waits_for_success_and_resolves_actual_identifier():
    executed = []
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="create",
                    kind="reference",
                    tool="create",
                    arguments={"name": "meeting"},
                ),
                ScenarioAction(
                    id="update",
                    kind="environment",
                    tool="update",
                    arguments={"id": "{{create}}"},
                    dependencies=["create"],
                    delay_seconds=3,
                ),
            ]
        ),
        lambda name, args: executed.append((name, args)),
    )
    runtime.observe("create", {"name": "meeting"}, "failed-id", error=True)
    runtime.advance(10)
    assert executed == []
    runtime.observe("create", {"name": "meeting"}, "actual-id")
    runtime.advance(12)
    assert executed == []
    runtime.advance(13)
    runtime.advance(100)
    assert executed == [("update", {"id": "actual-id"})]
    assert len([r for r in runtime.records if r.kind == "environment"]) == 1


def test_script_release_is_once_and_completion_requires_delivery():
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="request",
                    kind="user",
                    tool="user",
                    arguments={"content": "original request"},
                ),
                ScenarioAction(
                    id="later",
                    kind="environment",
                    tool="update",
                    dependencies=["request"],
                    delay_seconds=1,
                ),
            ]
        ),
        lambda *args: None,
    )
    runtime.advance(0)
    assert runtime.user_inputs.pop() == ("request", "original request")
    runtime.advance(2)
    assert runtime.user_inputs == []
    assert "later" not in runtime.completed
    runtime.delivered("user", "original request", event_id="request")
    runtime.advance(3)
    assert "later" in runtime.completed


def test_overdue_environment_updates_keep_causal_time_order():
    state = {}

    def update(name, args):
        state["value"] = args["value"]

    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="last",
                    kind="environment",
                    tool="update",
                    arguments={"value": 2},
                    delay_seconds=80,
                ),
                ScenarioAction(
                    id="first",
                    kind="environment",
                    tool="update",
                    arguments={"value": 1},
                    delay_seconds=40,
                ),
            ]
        ),
        update,
    )
    runtime.advance(100)
    assert state == {"value": 2}
    assert [r.event_id for r in runtime.records] == ["first", "last"]


def test_identical_operations_bind_in_due_order_without_enforcing_reference_delay():
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="later", kind="reference", tool="message", delay_seconds=80
                ),
                ScenarioAction(
                    id="earlier", kind="reference", tool="message", delay_seconds=40
                ),
            ]
        ),
        lambda *args: None,
    )
    runtime.advance(1)
    runtime.observe("message", {}, "first")
    assert runtime.results == {"earlier": "first"}
    assert runtime.completed["earlier"] == 1
    runtime.observe("message", {}, "second")
    assert runtime.results == {"earlier": "first", "later": "second"}


def test_binding_restarts_after_parent_and_uses_source_ordinal_for_ties():
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="input", kind="user", tool="user", arguments={"content": "go"}
                ),
                ScenarioAction(
                    id="z-first", kind="reference", tool="add", dependencies=["parent"]
                ),
                ScenarioAction(
                    id="parent", kind="reference", tool="ready", dependencies=["input"]
                ),
                ScenarioAction(
                    id="a-second", kind="reference", tool="add", dependencies=["parent"]
                ),
                ScenarioAction(
                    id="later",
                    kind="reference",
                    tool="add",
                    delay_seconds=80,
                    dependencies=["input"],
                ),
            ]
        ),
        lambda *args: None,
    )
    # The calls arrive before the input transport has bound the parent event.
    runtime.observe("ready", {}, None)
    runtime.observe("add", {}, "child-first")
    runtime.observe("add", {}, "child-second")
    runtime.observe("add", {}, "last")
    assert runtime.results == {}
    runtime.delivered("user", "go", event_id="input")
    assert runtime.results["z-first"] == "child-first"
    assert runtime.results["a-second"] == "child-second"
    assert runtime.results["later"] == "last"


def test_semantic_speech_matching_does_not_block_media_or_replay_models():
    future = Future()
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="question",
                    kind="reference",
                    tool="speech",
                    channel="agent",
                    arguments={"content": "Which property do you mean?"},
                ),
                ScenarioAction(
                    id="answer",
                    kind="user",
                    tool="user",
                    dependencies=["question"],
                    arguments={"content": "Property"},
                ),
            ]
        ),
        lambda *args: None,
    )

    class Executor:
        def submit(self, *args):
            return future

    runtime._match_executor = Executor()
    runtime.semantic_matcher = lambda *args: True
    runtime.delivered("agent", "Can you identify the property?")
    assert "question" not in runtime.completed
    assert runtime.user_inputs == []
    future.set_result(True)
    runtime.advance()
    assert runtime.user_inputs == [("answer", "Property")]
    assert any(r.kind == "anchor" for r in runtime.records)


def test_result_reference_decoder_never_executes_strings():
    malicious = "__import__('os').system('false')"
    assert resolve_references(malicious, {}) == malicious
    assert resolve_references({"ids": ["{{a}}"]}, {"a": 17}) == {"ids": [17]}
    with pytest.raises(KeyError):
        resolve_references("{{missing}}", {})


def test_tool_queue_keeps_scenario_events_running_and_executes_once():
    from tau2.data_model.message import ToolMessage

    state = []
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="arrival", kind="environment", tool="update", delay_seconds=0.03
                )
            ]
        ),
        lambda *args: state.append("arrived"),
    )

    class Environment:
        def get_response(self, call):
            updates = runtime.wait_for_notification(0.5)
            return ToolMessage(
                id=call.id,
                name=call.name,
                role="tool",
                requestor="assistant",
                content=str(updates),
            )

    executor = ToolExecutionQueue(Environment())
    call = ToolCall(id="once", name="wait", arguments={}, requestor="assistant")
    try:
        runtime.start()
        executor.submit([call])
        assert executor.drain("assistant") == []
        with pytest.raises(RuntimeError):
            executor.submit([call])
        deadline = time.monotonic() + 1
        ticks = 0
        results = []
        while not results and time.monotonic() < deadline:
            ticks += 1
            results = executor.drain("assistant")
            time.sleep(0.005)
        assert ticks > 1
        assert state == ["arrived"]
        assert [r.id for r in results] == ["once"]
        assert executor.drain("assistant") == []
    finally:
        runtime.close()
        executor.close()


def test_tool_effect_and_journal_cannot_be_overtaken_by_environment(monkeypatch):
    from tau2.environment.environment import Environment

    ready = threading.Event()
    attempted = threading.Event()
    acquired = threading.Event()
    effects = []
    runtime = ScenarioRuntime(spec([]), lambda *args: None)
    environment = Environment("mock", "test")
    environment.scenario_runtime = runtime

    def execute(call):
        effects.append("tool")
        ready.set()
        assert attempted.wait(1)
        return ToolMessage(
            role="tool",
            id=call.id,
            name=call.name,
            requestor="assistant",
            content='"ok"',
        )

    def external_event():
        assert ready.wait(1)
        attempted.set()
        with runtime.lock:
            acquired.set()
            effects.append("environment")
            runtime.record("environment", name="arrival")

    record = runtime.record

    def paused_journal(kind, **fields):
        if kind == "tool":
            # Give the waiting scheduler a chance to overtake an unlocked journal.
            acquired.wait(0.05)
        return record(kind, **fields)

    monkeypatch.setattr(environment, "_get_response", execute)
    monkeypatch.setattr(runtime, "record", paused_journal)
    worker = threading.Thread(target=external_event)
    worker.start()
    try:
        environment.get_response(
            ToolCall(id="c", name="write", arguments={}, requestor="assistant")
        )
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert effects == ["tool", "environment"]
        assert [row.kind for row in runtime.records] == effects
    finally:
        runtime.close()


def test_tool_replay_pairs_results_across_ticks():
    call = ToolCall(id="call", name="update", arguments={}, requestor="assistant")
    result = ToolMessage(
        id="call", name="update", content="ok", role="tool", requestor="assistant"
    )
    ticks = [
        Tick(tick_id=0, timestamp="0", agent_tool_calls=[call], environment_events=[]),
        Tick(
            tick_id=1, timestamp="1", agent_tool_results=[result], environment_events=[]
        ),
    ]
    messages = tool_messages(ticks)
    assert messages[0].tool_calls == [call]
    assert messages[1] == result
    assert "environment_events" not in Tick(tick_id=0, timestamp="0").model_dump()


def test_old_task_serialization_has_no_scenario_field():
    from tau2.data_model.tasks import Task, UserScenario

    task = Task(id="old", user_scenario=UserScenario(instructions="old instructions"))
    assert "scenario" not in task.model_dump(mode="json")


def test_early_effect_is_not_lost_while_its_parent_input_is_delivered():
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="input",
                    kind="user",
                    tool="user",
                    arguments={"content": "request"},
                ),
                ScenarioAction(
                    id="write",
                    kind="reference",
                    tool="write",
                    arguments={"value": 1},
                    dependencies=["input"],
                ),
                ScenarioAction(
                    id="arrival",
                    kind="environment",
                    tool="change",
                    dependencies=["write"],
                    delay_seconds=1,
                ),
            ]
        ),
        lambda *args: None,
    )
    runtime.observe("write", {"value": 1}, "actual-id")
    assert "write" not in runtime.completed
    runtime.delivered("user", "request", event_id="input")
    assert runtime.results["write"] == "actual-id"
    runtime.advance(1)
    assert "arrival" in runtime.completed


def test_textual_tool_anchors_bind_the_correct_actual_entity_after_reordering():
    changes = []
    runtime = ScenarioRuntime(
        spec(
            [
                ScenarioAction(
                    id="u",
                    kind="user",
                    tool="input",
                    arguments={"content": "Create both appointments"},
                ),
                ScenarioAction(
                    id="a",
                    kind="reference",
                    tool="create",
                    arguments={"day": 1, "title": "Topic A appointment"},
                    match_fields=["day"],
                    semantic_fields=["title"],
                    dependencies=["u"],
                ),
                ScenarioAction(
                    id="b",
                    kind="reference",
                    tool="create",
                    arguments={"day": 1, "title": "Topic B appointment"},
                    match_fields=["day"],
                    semantic_fields=["title"],
                    dependencies=["a"],
                ),
                ScenarioAction(
                    id="update",
                    kind="environment",
                    tool="change",
                    arguments={"id": "{{a}}"},
                    dependencies=["a"],
                    delay_seconds=1,
                ),
            ]
        ),
        lambda name, args: changes.append(args["id"]),
    )
    runtime.semantic_matcher = (
        lambda actual, expected: actual.replace("meeting", "appointment") == expected
    )
    try:
        runtime.delivered("user", "Create both appointments", event_id="u")
        runtime.observe(
            "create",
            {"day": 1, "title": "Topic B meeting"},
            "actual-b",
            call_id="b-first",
        )
        runtime.observe(
            "create",
            {"day": 1, "title": "Topic A meeting"},
            "actual-a",
            call_id="a-second",
        )
        deadline = time.monotonic() + 1
        while "b" not in runtime.completed and time.monotonic() < deadline:
            runtime.advance()
            time.sleep(0.002)
        assert runtime.results["a"] == "actual-a"
        assert runtime.results["b"] == "actual-b"
        runtime.advance(1)
        assert changes == ["actual-a"]
    finally:
        runtime.close()

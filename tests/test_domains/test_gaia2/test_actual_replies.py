"""Scripted replies follow real sent mail rather than oracle calendar effects."""

import copy
import json
from concurrent.futures import Future

import pytest

from tau2.data_model.message import ToolCall
from tau2.domains.gaia2.environment import get_environment, get_tasks
from tau2.environment.replay import replay_environment


def environment():
    task = next(t for t in get_tasks() if t.id.startswith("0867"))
    return get_environment(task=task, allow_unverified=True)


def send(env, content, recipients, identifier="send"):
    return env.get_response(
        ToolCall(
            id=identifier,
            name="Emails__send_email",
            arguments={
                "recipients": recipients,
                "subject": "Chess invitation",
                "content": content,
            },
            requestor="assistant",
        )
    )


def test_0867_replies_without_calendar_or_announcement_and_strict_replay():
    env = environment()
    rt = env.scenario_runtime
    original = copy.deepcopy(env.task.scenario)
    user = next(e for e in rt.spec.events if e.kind == "user" and not e.dependencies)
    rt.delivered("user", user.arguments["content"], event_id=user.id)
    rt.semantic_matcher = lambda actual, expected: "chess" in actual.lower()
    rules = rt.event_triggers.rules
    sent = {}
    try:
        for key, (event, origin, _) in rules.items():
            response = send(
                env, origin.arguments["content"], [event.arguments["sender"]], key
            )
            assert not response.error
            sent[key] = (
                json.loads(response.content)
                if response.content.startswith('"')
                else response.content
            )
        rt.advance(0.5)
        assert not [r for r in rt.records if r.kind == "environment"]
        rt.advance(2)
        rt.advance(3)
        replies = [r for r in rt.records if r.kind == "environment"]
        assert len(replies) == 2
        assert {r.event_id: r.arguments["email_id"] for r in replies} == sent
        assert env.task.scenario == original
        assert not any(k.startswith("OracleEvent-AGENT-253") for k in rt.completed)
        replayed = get_environment(
            task=env.task, allow_unverified=True, runtime_mode="replay"
        )
        try:
            replay_environment(
                replayed, [r.model_dump(mode="json") for r in rt.records]
            )
            for event in replies:
                assert replayed.scenario_runtime.results[event.event_id] == event.result
        finally:
            replayed.close()
    finally:
        env.close()


def test_wrong_recipient_content_and_failed_sends_never_release():
    env = environment()
    rt = env.scenario_runtime
    user = next(e for e in rt.spec.events if e.kind == "user" and not e.dependencies)
    rt.delivered("user", "go", event_id=user.id)
    rt.semantic_matcher = lambda a, b: False
    try:
        send(env, "Unrelated message", ["someone@example.com"], "wrong-recipient")
        send(env, "Unrelated message", ["elnovakova@student.cuni.cz"], "wrong-content")
        rt.observe("Emails__send_email", {}, "nonexistent", error=True)
        rt.advance(100)
        assert not rt.event_triggers.bindings
        assert not [r for r in rt.records if r.kind == "environment"]
    finally:
        env.close()


def test_match_waits_and_uses_send_time_not_late_match_time():
    env = environment()
    rt = env.scenario_runtime
    user = next(e for e in rt.spec.events if e.kind == "user" and not e.dependencies)
    rt.delivered("user", "go", event_id=user.id)
    key, (event, origin, _) = next(iter(rt.event_triggers.rules.items()))
    future = Future()

    class Executor:
        def submit(self, *a):
            return future

        def shutdown(self, **kw):
            pass

    rt._match_executor = Executor()
    rt.semantic_matcher = lambda a, b: True
    try:
        send(env, "Can we play chess?", [event.arguments["sender"]])
        rt.advance(20)
        assert key not in rt.event_triggers.bindings
        future.set_result(True)
        rt.advance(30)
        reply = next(r for r in rt.records if r.kind == "environment")
        assert reply.time == rt.spec.start_time + 30
        assert rt.event_triggers.bindings[key]["sent_time"] == rt.spec.start_time
        rt.advance(40)
        assert len([r for r in rt.records if r.kind == "environment"]) == 1
    finally:
        env.close()


def test_group_mail_can_bind_each_correspondent_and_does_not_rebind():
    env = environment()
    rt = env.scenario_runtime
    user = next(e for e in rt.spec.events if e.kind == "user" and not e.dependencies)
    rt.delivered("user", "go", event_id=user.id)
    rt.semantic_matcher = lambda a, b: True
    # Pre-resolve the async content decision so this test has no timing race.
    done = Future()
    done.set_result(True)

    class Executor:
        def submit(self, *a):
            return done

        def shutdown(self, **kw):
            pass

    rt._match_executor = Executor()
    recipients = [
        event.arguments["sender"] for event, _, _ in rt.event_triggers.rules.values()
    ]
    try:
        first = send(env, "Chess invitations", recipients, "first")
        rt.advance(0)
        bindings = copy.deepcopy(rt.event_triggers.bindings)
        assert len(bindings) == 2
        assert {b["mail_id"] for b in bindings.values()} == {first.content}
        send(env, "Chess invitations", recipients, "second")
        rt.advance(5)
        assert rt.event_triggers.bindings == bindings
        assert len([r for r in rt.records if r.kind == "environment"]) == 2
    finally:
        env.close()


def test_semantic_match_errors_remain_visible():
    env = environment()
    rt = env.scenario_runtime
    user = next(e for e in rt.spec.events if e.kind == "user" and not e.dependencies)
    rt.delivered("user", "go", event_id=user.id)
    rt.semantic_matcher = lambda a, b: True
    done = Future()
    done.set_exception(ValueError("judge unavailable"))

    class Executor:
        def submit(self, *a):
            return done

        def shutdown(self, **kw):
            pass

    rt._match_executor = Executor()
    event, _, _ = next(iter(rt.event_triggers.rules.values()))
    try:
        with pytest.raises(ValueError, match="judge unavailable"):
            send(env, "Chess invitation", [event.arguments["sender"]])
    finally:
        env.close()

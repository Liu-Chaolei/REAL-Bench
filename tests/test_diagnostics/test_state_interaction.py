"""Mechanism tests with fixed semantics; never call a live judge."""

import json
from types import SimpleNamespace

from tau2.data_model.diagnostics import DiagnosticEvidence, OperationEvidence
from tau2.diagnostics.state.events import recorded_events
from tau2.diagnostics.state.metrics import evaluate_state_interaction
from tau2.diagnostics.state.models import DialogueEvent, Interpretation
from tau2.diagnostics.state.projector import project_state
from tau2.diagnostics.state.semantic import SemanticInterpreter


class Interpreter:
    def __init__(self, rows):
        self.rows, self.inputs = rows, []

    def interpret(self, event, visible):
        self.inputs.append((event.event_id, visible))
        value = self.rows.get(event.event_id, {})
        return Interpretation.model_validate(
            value(event, visible) if callable(value) else value
        )


class WrappedJsonModel:
    def __init__(self, value):
        self.value = value

    def complete_sync(self, prompt, schema):
        return json.dumps({"type": "json_object", **self.value})


def ev(id, kind, content=None, role=None, view=None):
    role = role or ("user" if kind == "user_turn" else "agent")
    return dict(
        event_id=id,
        kind=kind,
        role=role,
        content=content or {},
        visible_to=view if view is not None else ["agent"],
        sequence=0,
    )


def evidence(events, operations=(), frames=None):
    return DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="mock",
        state_events=[dict(e, sequence=i) for i, e in enumerate(events)],
        operations=list(operations),
        frames=frames or {},
    )


def test_semantic_interpreter_accepts_provider_json_object_wrapper():
    event = DialogueEvent(**ev("agent", "agent_turn", "Hello"))
    interpreter = SemanticInterpreter(
        WrappedJsonModel({"greeting": True}), capabilities={}
    )

    result = interpreter.interpret(event, visible={})

    assert result.greeting is True


def agenda(id="answer", expected=6):
    return dict(
        id=id,
        kind="interaction",
        key="answer",
        value="Answer question",
        completion=dict(mode="communication", expected=expected),
    )


def use(id="answer", actual=6, **kwargs):
    return dict(
        dict(
            item_id=id,
            mode="reply",
            actual=actual,
            status="consistent",
            reason="uses_current_state",
        ),
        **kwargs,
    )


def test_private_task_data_is_not_initial_agent_knowledge():
    task = SimpleNamespace(
        user_scenario=SimpleNamespace(instructions="Future secret"),
        evaluation_criteria=SimpleNamespace(communicate_info=["hidden answer"]),
    )
    interpreter = Interpreter({})
    _, _, state = project_state(
        task, evidence([ev("a", "agent_turn", "Hello")]), interpreter
    )
    visible = interpreter.inputs[0][1]
    assert "Future secret" not in str(visible) and "hidden answer" not in str(visible)
    assert not state.facts and len(state.agenda) == 1


def test_numeric_completion_and_repetition_are_state_edges():
    events = [
        ev("u", "user_turn", "What is the answer?"),
        ev("a", "agent_turn", "9"),
        ev("b", "agent_turn", "6"),
        ev("c", "agent_turn", "6"),
    ]
    semantics = {"u": {"updates": [agenda()]}}
    for id, value in [("a", 9), ("b", 6), ("c", 6)]:
        semantics[id] = dict(
            uses=[use(actual=value)],
            fulfills=[dict(item_id="answer", actual=value, met=True)],
        )
    windows, versions, state = project_state(
        None, evidence(events), Interpreter(semantics)
    )
    assert windows[0].delta.satisfied_items == []
    assert windows[1].delta.satisfied_items == ["answer"]
    assert windows[2].delta.satisfied_items == []
    assert state.agenda["answer"].status == "satisfied"
    assert windows[0].state_before_agent == 1
    assert windows[0].state_before_agent_reply["a"] == 1
    assert len(versions) == len(events) + 1


def test_user_fact_and_irrelevant_success_are_not_agent_progress():
    events = [
        ev("u", "user_turn", "Send to Alice"),
        ev(
            "call",
            "tool_requested",
            dict(call_id="c", name="Weather__query", arguments={}),
        ),
        ev(
            "done",
            "tool_completed",
            dict(call_id="c", result={"sunny": True}, error=False),
            role="environment",
            view=[],
        ),
        ev(
            "out",
            "tool_delivered",
            dict(call_id="c", result={"sunny": True}, error=False),
        ),
        ev("a", "agent_turn", "What computer?"),
    ]
    interpreter = Interpreter(
        {
            "u": dict(
                updates=[
                    dict(id="recipient", kind="fact", key="recipient", value="Alice")
                ]
            )
        }
    )
    windows, _, state = project_state(None, evidence(events), interpreter)
    assert windows[0].progressivity == "stagnated"
    assert state.operations["operation:c"].status == "succeeded"


def test_future_completion_never_supports_past_claim_and_unknown_delivery_stays_hidden():
    events = [
        ev(
            "call",
            "tool_requested",
            dict(call_id="c", name="Email__send", arguments={}),
        ),
        ev("a", "agent_turn", "Sent"),
        ev(
            "done",
            "tool_completed",
            dict(call_id="c", result="sent", error=False),
            role="environment",
            view=[],
        ),
        ev("b", "agent_turn", "Sent"),
        ev(
            "delivered", "tool_delivered", dict(call_id="c", result="sent", error=False)
        ),
        ev("d", "agent_turn", "Sent"),
    ]
    interpreter = Interpreter(
        {
            k: dict(uses=[use(id="operation:c", actual="succeeded")])
            for k in ("a", "b", "d")
        }
    )
    windows, _, _ = project_state(None, evidence(events), interpreter)
    assert windows[0].opportunities[0]["status"] == "unsupported"
    assert windows[1].opportunities[0]["status"] == "unsupported"
    assert windows[2].opportunities[0]["status"] == "consistent"
    assert "sent" not in str(
        interpreter.inputs[2][1]["operations"]["operation:c"]["value"]
    )


def test_correction_keeps_history_and_grounding_detects_stale_actual():
    rows = {
        "u": dict(
            updates=[dict(id="recipient", kind="fact", key="recipient", value="Alice")]
        ),
        "u2": dict(
            updates=[
                dict(
                    id="recipient",
                    kind="fact",
                    key="recipient",
                    value="Bob",
                    mode="correct",
                )
            ]
        ),
        "a": dict(uses=[use(id="recipient", actual="Alice")]),
    }
    windows, _, state = project_state(
        None,
        evidence(
            [
                ev("u", "user_turn", "Alice"),
                ev("u2", "user_turn", "Actually Bob"),
                ev("a", "agent_turn", "Alice"),
            ]
        ),
        Interpreter(rows),
    )
    assert windows[0].opportunities[0]["status"] == "inconsistent"
    assert windows[0].opportunities[0]["reason"] == "stale_state_use"
    assert state.facts["recipient"].history[0]["value"] == "Alice"
    assert state.facts["recipient"].value == "Bob"


def test_greeting_once_and_state_use_opportunities_not_window_labels(tmp_path):
    events = [ev("a", "agent_turn", "Hello"), ev("b", "agent_turn", "Hello")]
    interpreter = Interpreter(
        {
            "a": dict(greeting=True),
            "b": dict(greeting=True, uses=[use(id="missing", status="unsupported")]),
        }
    )
    report = evaluate_state_interaction(
        None, evidence(events), tmp_path, interpreter=interpreter
    )
    assert report["progressivity"]["forward_rate"] == 0.5
    assert report["grounding"]["consistent_rate"] == 0
    assert (tmp_path / "state_trace.json").exists()
    assert (tmp_path / "state_interaction.json").exists()


def test_external_waiting_resolves_and_only_matching_outcome_completes():
    item = dict(
        id="send",
        kind="business",
        key="send",
        value="Send",
        completion=dict(mode="tool_result", tool="Email__send", comparison="success"),
    )
    events = [
        ev("u", "user_turn", "Send"),
        ev("c1", "tool_requested", dict(call_id="1", name="Email__send", arguments={})),
        ev(
            "d1",
            "tool_delivered",
            dict(call_id="1", error=True, result={"code": "unavailable"}),
        ),
        ev("a", "agent_turn", "Waiting"),
        ev("c2", "tool_requested", dict(call_id="2", name="Email__send", arguments={})),
        ev("d2", "tool_delivered", dict(call_id="2", error=False, result="sent")),
        ev("b", "agent_turn", "Done"),
    ]
    rows = {"u": dict(updates=[item])}
    for id in ("c1", "c2"):
        rows[id] = dict(
            uses=[use(id="send", mode="tool_action")], related_items=["send"]
        )
    windows, _, state = project_state(None, evidence(events), Interpreter(rows))
    assert windows[0].progressivity == "externally_blocked"
    assert windows[1].progressivity == "forward"
    assert state.agenda["send"].blocked_by is None
    assert windows[1].delta.satisfied_items == ["send"]


def test_text_order_uses_messages_and_call_ids_not_runtime_seconds():
    from tau2.data_model.message import (
        AssistantMessage,
        ToolCall,
        ToolMessage,
        UserMessage,
    )

    op = OperationEvidence(
        call_id="c",
        name="send",
        app="Email",
        kind="WRITE",
        raw_arguments={},
        arguments={},
        result="ok",
        started=5000,
        ended=5010,
    )
    sim = SimpleNamespace(
        ticks=[],
        messages=[
            UserMessage(role="user", content="Send"),
            AssistantMessage(
                role="assistant",
                tool_calls=[ToolCall(id="c", name="send", arguments={})],
            ),
            ToolMessage(role="tool", id="c", requestor="assistant", content="ok"),
            AssistantMessage(role="assistant", content="Sent"),
        ],
    )
    events = recorded_events(sim, evidence([], operations=[op]), [])
    assert [r["kind"] for r in events] == [
        "user_turn",
        "tool_requested",
        "tool_completed",
        "tool_delivered",
        "agent_turn",
    ]
    assert events[3]["content"]["call_id"] == "c"
    assert all(r["timestamp"] is None for r in events)


def test_candidates_and_legitimate_cancel_do_not_count_as_regression():
    rows = {
        "u": dict(
            updates=[
                dict(id="g", kind="goal", key="send", value="Send"),
                dict(
                    id="recipient",
                    kind="fact",
                    key="recipient",
                    mode="candidates",
                    candidates=["Alice", "Bob"],
                ),
            ]
        ),
        "v": dict(updates=[dict(id="g", kind="goal", key="send", mode="cancel")]),
    }
    windows, _, state = project_state(
        None,
        evidence(
            [
                ev("u", "user_turn", "One of them"),
                ev("v", "user_turn", "Cancel"),
                ev("a", "agent_turn", "Okay"),
            ]
        ),
        Interpreter(rows),
    )
    assert state.facts["recipient"].status == "conflicted"
    assert state.goals["g"].status == "cancelled"
    assert not windows[0].delta.regressed_items


def test_grounding_checks_actions_and_ignores_unrelated_state_items():
    events = [
        ev("u", "user_turn", "Send to Bob"),
        ev(
            "c",
            "tool_requested",
            dict(call_id="c", name="Email__send", arguments={"recipient": "Alice"}),
        ),
    ]
    rows = {
        "u": dict(
            updates=[
                dict(id="recipient", kind="fact", key="recipient", value="Bob"),
                dict(id="attachment", kind="fact", key="attachment", value="a.pdf"),
            ]
        ),
        "c": dict(uses=[use(id="recipient", mode="tool_action", actual="Alice")]),
    }
    windows, _, _ = project_state(None, evidence(events), Interpreter(rows))
    opportunities = windows[0].opportunities
    assert len(opportunities) == 1
    assert opportunities[0]["status"] == "inconsistent"
    assert opportunities[0]["item_id"] == "recipient"


def test_component_result_is_not_visible_to_frontend_until_forwarded():
    events = [
        ev(
            "c",
            "tool_requested",
            dict(call_id="c", name="Email__send", arguments={}),
            role="backend",
            view=["backend"],
        ),
        ev(
            "d",
            "tool_delivered",
            dict(call_id="c", result="sent", error=False),
            role="backend",
            view=["backend"],
        ),
        ev("a", "agent_turn", "Sent", role="frontend", view=["frontend"]),
    ]
    interpreter = Interpreter(
        {"a": dict(uses=[use(id="operation:c", actual="succeeded")])}
    )
    windows, _, _ = project_state(None, evidence(events), interpreter)
    assert windows[0].opportunities[0]["status"] == "uncertain"
    assert not interpreter.inputs[-1][1]["operations"]


def test_result_answer_uses_visible_tool_value_instead_of_private_reference():
    events = [
        ev("u", "user_turn", "How many?"),
        ev("c", "tool_requested", dict(call_id="c", name="Count", arguments={})),
        ev("d", "tool_delivered", dict(call_id="c", error=False, result={"count": 6})),
        ev("a", "agent_turn", "6"),
    ]
    row = agenda(expected=None)
    rows = {
        "u": dict(updates=[row]),
        "a": dict(
            uses=[use(evidence_ids=["d"])],
            fulfills=[
                dict(
                    item_id="answer",
                    actual=6,
                    met=True,
                    evidence_id="operation:c",
                    evidence_path="/count",
                )
            ],
        ),
    }
    windows, _, state = project_state(None, evidence(events), Interpreter(rows))
    assert state.agenda["answer"].status == "satisfied"
    assert windows[0].delta.satisfied_items == ["answer"]


def test_state_effects_allow_alternative_tools_and_check_actual_outcome():
    item = dict(
        id="target",
        kind="business",
        key="target",
        value="Set value",
        completion=dict(mode="state_effect", path="/App/value", expected=6),
    )
    op = OperationEvidence(
        call_id="c",
        name="AlternativeTool",
        app="App",
        kind="WRITE",
        raw_arguments={},
        arguments={},
        result="ok",
        started=1,
        ended=2,
        after_frame="after",
    )
    events = [
        ev("u", "user_turn", "Set value"),
        ev(
            "c",
            "tool_requested",
            dict(call_id="c", name="AlternativeTool", arguments={}),
        ),
        ev("d", "tool_delivered", dict(call_id="c", result="ok", error=False)),
        ev("a", "agent_turn", "Done"),
    ]
    rows = {
        "u": dict(updates=[item]),
        "c": dict(
            uses=[use(id="target", mode="tool_action")], related_items=["target"]
        ),
    }
    windows, _, state = project_state(
        None,
        evidence(events, [op], {"after": {"App": {"value": 6}}}),
        Interpreter(rows),
    )
    assert state.agenda["target"].status == "satisfied"
    assert windows[0].progressivity == "forward"


def test_one_report_contains_state_and_other_measures_without_extra_run(
    monkeypatch, tmp_path
):
    import json

    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.model import TauJsonModel
    from tau2.diagnostics.suite import evaluate_evidence, summarize

    events = [ev("u", "user_turn", "Hello"), ev("a", "agent_turn", "Hello")]
    inputs = []

    def fake(self, prompt, schema, **kwargs):
        inputs.append(prompt)
        return json.dumps(dict(greeting='"kind":"agent_turn"' in prompt))

    monkeypatch.setattr(TauJsonModel, "complete_sync", fake)
    task = SimpleNamespace(model_dump=lambda **_: {})
    report = evaluate_evidence(
        task,
        evidence(events),
        tmp_path / "evidence",
        MetricsConfig(measures=["state_interaction", "voice"]),
    )
    assert set(report["summary"]["measures"]) == {"state_interaction", "voice"}
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert "state_interaction" in latest["summary"]["measures"]
    assert (
        summarize([report])["metrics"]["state_interaction"]["progressivity"][
            "forward_rate"
        ]
        == 1
    )
    assert len(inputs) == 2
    # Cache preserves the same complete sidecar, without a second interpretation.
    evaluate_evidence(
        task,
        evidence(events),
        tmp_path / "evidence",
        MetricsConfig(measures=["state_interaction", "voice"]),
    )
    assert len(inputs) == 2


def test_semantic_failure_leaves_unknown_window_without_fabricated_zero(tmp_path):
    class Broken:
        def interpret(self, *args):
            raise RuntimeError("offline")

    report = evaluate_state_interaction(
        None, evidence([ev("a", "agent_turn", "Hi")]), tmp_path, interpreter=Broken()
    )
    assert report["status"] == "partial"
    assert report["progressivity"]["forward_rate"] is None
    assert report["grounding"]["consistent_rate"] is None


def test_successful_status_query_does_not_prove_queried_task_completed():
    events = [
        ev("c", "tool_requested", dict(call_id="c", name="TaskStatus", arguments={})),
        ev(
            "d",
            "tool_delivered",
            dict(call_id="c", result={"state": "running"}, error=False),
        ),
        ev("a", "agent_turn", "Task is complete"),
    ]
    rows = {
        "a": dict(
            uses=[use(id="operation:c", actual="completed", path="/result/state")]
        )
    }
    windows, _, _ = project_state(None, evidence(events), Interpreter(rows))
    assert windows[0].opportunities[0]["status"] == "inconsistent"


def test_completed_agenda_is_not_future_evidence_for_other_component():
    item = dict(
        id="send",
        kind="business",
        key="send",
        value="Send",
        completion=dict(mode="tool_result", tool="EmailSend", comparison="success"),
    )
    events = [
        ev("u", "user_turn", "Send", view=["frontend"]),
        ev(
            "c",
            "tool_requested",
            dict(call_id="c", name="EmailSend", arguments={}),
            role="backend",
            view=["backend"],
        ),
        ev(
            "d",
            "tool_delivered",
            dict(call_id="c", result="ok", error=False),
            role="backend",
            view=["backend"],
        ),
        ev("a", "agent_turn", "Sent", role="frontend", view=["frontend"]),
    ]
    interpreter = Interpreter(
        {
            "u": dict(updates=[item]),
            "c": dict(
                uses=[use(id="send", mode="tool_action")], related_items=["send"]
            ),
        }
    )
    _, _, state = project_state(None, evidence(events), interpreter)
    assert state.agenda["send"].status == "satisfied"
    assert interpreter.inputs[-1][1]["agenda"]["send"]["status"] == "open"
    assert "d" not in interpreter.inputs[-1][1]["agenda"]["send"]["source_refs"]


def test_voice_uses_local_delivery_clock_not_scenario_seconds():
    sim = SimpleNamespace(ticks=[object()])
    op = OperationEvidence(
        call_id="c",
        name="Send",
        app="Email",
        kind="WRITE",
        raw_arguments={},
        arguments={},
        result="ok",
        started=9999,
        ended=10000,
    )
    from tau2.data_model.diagnostics import UserInput

    e = evidence([], operations=[op])
    e.audio = {"activity": {"origin": 100}}
    e.user_inputs = [
        UserInput(id="a", role="assistant_context", text="Sent", started=1, ended=1.2)
    ]
    records = [
        dict(
            event_id="c",
            kind="tool_requested",
            role="backend",
            content=dict(call_id="c", name="Send", arguments={}),
            visible_to=["backend"],
            timestamp=100.5,
            metadata={},
        ),
        dict(
            event_id="d",
            kind="tool_delivered",
            role="backend",
            content=dict(call_id="c"),
            visible_to=["backend"],
            timestamp=101.5,
            metadata={},
        ),
    ]
    events = recorded_events(sim, e, records)
    assert [r["kind"] for r in events] == [
        "tool_requested",
        "agent_turn",
        "tool_delivered",
    ]
    assert events[-1]["content"]["result"] == "ok"


def test_mixed_state_versions_are_separate_in_summary():
    from tau2.diagnostics.suite import summarize

    rows = [
        dict(
            task_id="a",
            summary={
                "measures": {
                    "state_interaction": dict(
                        version=v,
                        status="complete",
                        progressivity={"forward_rate": value},
                        grounding={"consistent_rate": value},
                    )
                }
            },
        )
        for v, value in [("tau-state-interaction/1", 0), ("tau-state-interaction/2", 1)]
    ]
    report = summarize(rows)
    assert len(report["groups"]) == 2
    assert (
        report["groups"]["tau-state-interaction/2"]["metrics"]["state_interaction"][
            "progressivity"
        ]["forward_rate"]
        == 1
    )


def test_missing_required_phase_evidence_is_unknown_not_stagnation():
    item = dict(
        id="x",
        kind="business",
        key="x",
        value="Change value",
        completion=dict(mode="state_effect", path="/App/value", expected=6),
    )
    rows = {
        "u": dict(updates=[item]),
        "c": dict(uses=[use(id="x", mode="tool_action")], related_items=["x"]),
    }
    events = [
        ev("u", "user_turn", "Change"),
        ev("c", "tool_requested", dict(call_id="c", name="Change", arguments={})),
        ev("d", "tool_delivered", dict(call_id="c", result="ok", error=False)),
        ev("a", "agent_turn", "Done"),
    ]
    windows, _, _ = project_state(None, evidence(events), Interpreter(rows))
    assert not windows[0].scoreable
    assert windows[0].issues[0]["reason"] == "outcome_evidence_unavailable"


def test_first_denial_is_not_converted_to_positive_known_fact():
    rows = {
        "u": dict(
            updates=[
                dict(
                    id="permission",
                    kind="fact",
                    key="permission",
                    value="granted",
                    mode="deny",
                )
            ]
        ),
        "a": dict(uses=[use(id="permission", actual="granted")]),
    }
    windows, _, state = project_state(
        None,
        evidence(
            [ev("u", "user_turn", "No permission"), ev("a", "agent_turn", "Authorized")]
        ),
        Interpreter(rows),
    )
    assert state.facts["permission"].status == "denied"
    assert windows[0].opportunities[0]["status"] == "inconsistent"

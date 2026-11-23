import json
from pathlib import Path

from tau2.data_model.hidden_intent import HiddenGoalGraph
from tau2.data_model.message import AssistantMessage
from tau2.domains.gaia2_hidden.loader import (
    get_goal_graph,
    get_tasks,
    get_tasks_split,
)
from tau2.user.hidden_intent import HiddenIntentRuntime
from tau2.user.user_simulator import UserSimulator

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/tau2/domains/gaia2_hidden"


def test_hidden_package_has_balanced_selection_and_valid_tasks():
    tasks = get_tasks("base")
    assert len(tasks) == 100
    assert {task.scenario.category for task in tasks} == {"hidden_intent"}
    assert len({task.scenario.source_id for task in tasks}) == 100
    assert len(get_tasks_split()["hidden_intent"]) == 100


def test_hidden_package_manifest_matches_provenance():
    manifest = json.loads((DATA / "manifest.json").read_text())
    provenance = json.loads((DATA / "provenance.json").read_text())
    plans = json.loads((DATA / "hidden_requirements.json").read_text())
    assert manifest["tasks"] == 100
    assert manifest["categories"] == {
        "search": 20,
        "execution": 20,
        "ambiguity": 20,
        "adaptability": 20,
        "time": 20,
    }
    assert set(provenance) == set(item["task_id"] for item in plans)


def test_llm_detector_rejects_summary_labels_and_accepts_direct_question(monkeypatch):
    task = next(
        item for item in get_tasks("hidden_intent") if item.id.startswith("hidden_0601")
    )
    graph = HiddenGoalGraph.model_validate(
        get_goal_graph(task.id)
    )
    runtime = HiddenIntentRuntime(graph)

    monkeypatch.setattr(
        "tau2.user.hidden_intent.generate",
        lambda **_: AssistantMessage(
            role="assistant", content='{"action":"no_release","node_id":null}'
        ),
    )
    summary = runtime.detect_and_apply(
        "Title: Outreach priorities outside Prague\nWhen: Friday, October 18",
        model="test-model",
        model_args={},
    )
    assert summary["released"] is False
    assert runtime.state.released_values == {}

    monkeypatch.setattr(
        "tau2.user.hidden_intent.generate",
        lambda **_: AssistantMessage(
            role="assistant",
            content='{"action":"release","node_id":"g_hidden_1"}',
        ),
    )
    question = runtime.detect_and_apply(
        "What should I call the calendar block?",
        model="test-model",
        model_args={},
    )
    assert question["released"] is True
    assert runtime.state.asked_nodes == ["g_hidden_1"]


def test_llm_detector_failure_keeps_field_withheld(monkeypatch):
    graph = HiddenGoalGraph.model_validate(
        {
            "source_task_id": "source",
            "source_category": "execution",
            "nodes": [
                {
                    "id": "g_source_request",
                    "kind": "communication",
                    "visibility": "caller_visible",
                    "description": "Continue the source task",
                },
                {
                    "id": "g_title",
                    "kind": "tool_argument",
                    "visibility": "withheld",
                    "field_type": "group_title",
                    "description": "The calendar title",
                    "aliases": ["title"],
                    "gold_value": "Private title",
                    "depends_on": ["g_source_request"],
                    "release_policy": "after_valid_question",
                },
            ],
        }
    )
    runtime = HiddenIntentRuntime(graph)

    def fail(**_):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr("tau2.user.hidden_intent.generate", fail)
    result = runtime.detect_and_apply(
        "What should I call it?", model="test-model", model_args={}
    )
    assert result["reason"] == "detector_error"
    assert runtime.state.node_states["g_title"] == "withheld"


def test_hidden_detector_deactivates_after_stage_zero():
    graph = HiddenGoalGraph.model_validate(
        {
            "source_task_id": "source",
            "source_category": "execution",
            "target_stage": 0,
            "nodes": [
                {
                    "id": "g_source_request",
                    "kind": "communication",
                    "visibility": "caller_visible",
                    "description": "Continue the source task",
                }
            ],
        }
    )
    runtime = HiddenIntentRuntime(graph)
    assert runtime.active is True
    runtime.deactivate()
    assert (
        runtime.detect_and_apply(
            "What should I call the later request?", model="unused", model_args={}
        )["reason"]
        == "inactive_stage"
    )


def test_hidden_graph_releases_only_after_a_matching_question():
    graph = HiddenGoalGraph.model_validate(
        {
            "source_task_id": "source",
            "source_category": "search",
            "nodes": [
                {
                    "id": "g_source_request",
                    "kind": "communication",
                    "visibility": "caller_visible",
                    "description": "Continue the source task",
                },
                {
                    "id": "g_subject",
                    "kind": "tool_argument",
                    "visibility": "withheld",
                    "field_type": "email_subject",
                    "description": "Email subject",
                    "aliases": [
                        "subject",
                        "email subject",
                        "what should the subject say",
                    ],
                    "argument_path": "subject",
                    "gold_value": "Subject A",
                    "depends_on": ["g_source_request"],
                    "release_policy": "after_valid_question",
                },
            ],
        }
    )
    runtime = HiddenIntentRuntime(graph)
    assert runtime.ask("Can you tell me the recipient?").get("released") is False
    assert runtime.visible_values() == {}
    released = runtime.ask("What email subject should I use?", node_id="g_subject")
    assert released["released"] is True
    assert runtime.visible_values() == {"g_subject": "Subject A"}


def test_natural_questions_release_then_use_the_normal_user_llm(monkeypatch):
    graph = HiddenGoalGraph.model_validate(
        {
            "source_task_id": "source",
            "source_category": "execution",
            "nodes": [
                {
                    "id": "g_source_request",
                    "kind": "communication",
                    "visibility": "caller_visible",
                    "description": "Continue the source task",
                },
                {
                    "id": "g_amount",
                    "kind": "tool_argument",
                    "visibility": "withheld",
                    "field_type": "amount",
                    "description": "Price threshold",
                    "aliases": ["price threshold", "budget", "maximum price"],
                    "gold_value": "$3,000",
                    "release_policy": "after_valid_question",
                },
            ],
        }
    )
    user = UserSimulator(
        llm="test-model", instructions="caller", llm_args={}, hidden_intent_graph=graph
    )
    monkeypatch.setattr(
        "tau2.user.user_simulator.generate",
        lambda **_: AssistantMessage(
            role="assistant", content="I can use the amount you provided."
        ),
    )
    monkeypatch.setattr(
        user.hidden_intent_runtime,
        "detect_and_apply",
        lambda question, **_: user.hidden_intent_runtime.ask_auto(question),
    )
    state = user.get_init_state()
    message, _ = user.generate_next_message(
        AssistantMessage(
            role="assistant", content="What price threshold should I use?"
        ),
        state,
    )
    assert message.content == "I can use the amount you provided."
    assert message.raw_data is None
    assert user.hidden_intent_runtime.state.node_states["g_amount"] == "released"
    private_context = "\n".join(item.content or "" for item in state.system_messages)
    assert "$3,000" in private_context
    assert "status=released" in private_context


def test_tool_binding_marks_satisfied_and_records_premature_action():
    graph = HiddenGoalGraph.model_validate(
        {
            "source_task_id": "source",
            "source_category": "execution",
            "nodes": [
                {
                    "id": "g_source_request",
                    "kind": "communication",
                    "visibility": "caller_visible",
                    "description": "Continue the source task",
                },
                {
                    "id": "g_amount",
                    "kind": "tool_argument",
                    "visibility": "withheld",
                    "field_type": "amount",
                    "description": "Price threshold",
                    "aliases": ["price threshold"],
                    "tool": "Apartment__search",
                    "argument_path": "max_price",
                    "gold_value": "$3,000",
                    "release_policy": "after_valid_question",
                },
            ],
        }
    )
    runtime = HiddenIntentRuntime(graph)
    runtime.observe_tool_call("Apartment__search", {"max_price": "$2,500"})
    assert runtime.state.transitions[-1]["type"] == "premature_action"
    runtime.ask_auto("What price threshold should I use?")
    assert runtime.observe_tool_call("Apartment__search", {"max_price": "$3,000"}) == [
        "g_amount"
    ]
    assert runtime.state.node_states["g_amount"] == "satisfied"


def test_hidden_value_is_not_in_user_instructions():
    tasks = json.loads((DATA / "tasks.json").read_text())
    hidden_task = next(item for item in tasks if item["id"].startswith("hidden_"))
    instructions = json.dumps(hidden_task["user_scenario"], ensure_ascii=False)
    plans = json.loads((DATA / "hidden_requirements.json").read_text())
    plan = next(item for item in plans if item["task_id"] == hidden_task["id"])
    for requirement in plan["hidden_requirements"]:
        assert str(requirement["gold_value"]) not in instructions


def test_agent_constructor_does_not_receive_hidden_graph(monkeypatch):
    import inspect

    from tau2.runner.build import build_agent

    assert "hidden_intent_graph" not in inspect.signature(build_agent).parameters

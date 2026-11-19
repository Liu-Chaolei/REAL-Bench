import json
from pathlib import Path

from tau2.data_model.hidden_intent import HiddenGoalGraph
from tau2.domains.gaia2_hidden.loader import get_tasks, get_tasks_split
from tau2.user.hidden_intent import HiddenIntentRuntime

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
                    "description": "Email subject",
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

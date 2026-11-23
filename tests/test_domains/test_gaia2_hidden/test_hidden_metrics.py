from tau2.data_model.hidden_intent import HiddenGoalGraph
from tau2.metrics.hidden_intent import score_hidden_intent
from tau2.user.hidden_intent import HiddenIntentRuntime


def test_hidden_metrics_are_replayable_and_keep_task_reward_separate():
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
                    "id": "g_subject",
                    "kind": "tool_argument",
                    "visibility": "withheld",
                    "field_type": "email_subject",
                    "description": "Email subject",
                    "aliases": ["subject", "email subject"],
                    "argument_path": "subject",
                    "gold_value": "Subject A",
                    "release_policy": "after_valid_question",
                },
            ],
        }
    )
    runtime = HiddenIntentRuntime(graph)
    runtime.ask("What email subject should I use?", node_id="g_subject")
    metrics = score_hidden_intent(graph, runtime.state, task_passed=True)
    assert metrics["hidden_ask_rate"] == 1.0
    assert set(metrics) == {"version", "hidden_count", "hidden_ask_rate"}

import copy

import pytest

from scripts.gaia2_replay_export import normalize_export


def test_normalization_preserves_original_actions_and_completed_history():
    original = {"event_id": "oracle", "event_type": "AGENT", "dependencies": []}
    derived = {
        "event_id": "condition_turn_1-CHECK_0",
        "event_type": "CONDITION",
        "class_name": "ConditionCheckEvent",
        "action": {
            "app": None,
            "function": "wrapped_condition__turn_idx_1__scenario_id_test",
        },
    }
    data = {"events": [original, derived], "completed_events": [derived, original]}
    before = copy.deepcopy(data)
    normalized, removed = normalize_export(data, {"events": [original]})
    assert data == before
    assert normalized["events"] == [original]
    assert normalized["completed_events"] == data["completed_events"]
    assert removed == [derived["event_id"]]
    kept, removed = normalize_export(data, {"events": [original, derived]})
    assert kept == data and not removed


def test_unknown_conditions_cannot_be_silently_removed():
    with pytest.raises(ValueError, match="Unrecognized"):
        normalize_export(
            {"events": [{"event_id": "unknown", "event_type": "CONDITION"}]},
            {"events": []},
        )


def test_restores_original_dependencies_without_altering_completed_events():
    source = {
        "events": [
            {"event_id": "env", "event_type": "ENV", "dependencies": ["oracle"]},
            {"event_id": "oracle", "event_type": "AGENT"},
        ]
    }
    data = copy.deepcopy(source)
    data["events"][0]["dependencies"] = ["condition_turn_1-CHECK_106"]
    data["completed_events"] = copy.deepcopy(data["events"])
    normalized, _ = normalize_export(data, source)
    assert normalized["events"] == source["events"]
    assert normalized["completed_events"] == data["completed_events"]


def test_empty_string_import_preserves_value_without_weakening_none_validation():
    from scripts.gaia2_replay_export import parse_replay_parameter

    def original(value, type_str):
        if type_str == "str" and value is None:
            raise ValueError("Non-optional str")
        return (value, type_str)

    assert parse_replay_parameter(original, "", "str") == ""
    assert parse_replay_parameter(original, "", "Optional[str]") == (
        "",
        "Optional[str]",
    )
    with pytest.raises(ValueError):
        parse_replay_parameter(original, None, "str")

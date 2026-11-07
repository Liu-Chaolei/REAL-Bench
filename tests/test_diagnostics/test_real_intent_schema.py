from copy import deepcopy

import pytest
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from tau2.diagnostics.intent.config import IntentConfig, ValidationProfile
from tau2.diagnostics.intent.units import build_items
from tau2.diagnostics.intent.validation import validate_intent

CONFIG = IntentConfig(definition_set="gaia2")
(SCHEMA, PROFILE, VALIDATION) = (
    CONFIG.load_schema(),
    CONFIG.profile(),
    CONFIG.validation(),
)


def checked(doc):
    validator_for(SCHEMA)(SCHEMA).validate(doc)
    return validate_intent(doc, VALIDATION)


def selected(quantifier):
    return {
        "goals": [
            {
                "id": "g",
                "action": "choose_product",
                "target": {
                    "kind": "selection",
                    "selection": {"entity_type": "product", "quantifier": quantifier},
                },
            }
        ]
    }


@pytest.mark.parametrize(
    "action",
    [
        "create",
        "create_event",
        "schedule_meeting",
        "find_meeting",
        "purchase",
        "send_chat",
        "derive_summary",
    ],
)
def test_open_actions_and_generic_outputs(action):
    doc = {
        "entities": [{"id": "x", "entity_type": "result_object"}],
        "goals": [
            {
                "id": "g",
                "action": action,
                "outputs": [{"entity_ref": "x", "role": "custom_business_result"}],
            }
        ],
    }
    checked(doc)
    units = build_items(doc, PROFILE, VALIDATION)
    assert [unit["unit_type"] for unit in units] == [
        "attribute",
        "action",
        "output_binding",
    ]
    assert build_items(doc, PROFILE) == units


def test_two_operations_may_return_same_existing_entity():
    doc = {
        "entities": [{"id": "meeting", "entity_type": "event"}],
        "goals": [
            {
                "id": "a",
                "action": "find_meeting",
                "outputs": [{"entity_ref": "meeting", "role": "query_result"}],
            },
            {
                "id": "b",
                "action": "read_details",
                "outputs": [{"entity_ref": "meeting", "role": "returned_object"}],
            },
        ],
    }
    checked(doc)
    assert (
        sum((u["unit_type"] == "output_binding" for u in build_items(doc, PROFILE)))
        == 2
    )


def test_unused_entity_and_inline_target_duplicate_is_rejected(duplicate_target_intent):
    original = deepcopy(duplicate_target_intent)
    with pytest.raises(
        ValueError,
        match="Unused entity /entities/0 duplicates /goals/0/target/selection",
    ):
        checked(duplicate_target_intent)
    assert duplicate_target_intent == original


@pytest.mark.parametrize("representation", ["reference", "inline_only"])
def test_duplicate_target_can_use_either_single_representation(
    duplicate_target_intent, representation
):
    if representation == "reference":
        duplicate_target_intent["goals"][0]["target"] = {
            "kind": "entity_ref",
            "ref": "contacts",
        }
    else:
        duplicate_target_intent.pop("entities")
    checked(duplicate_target_intent)


@pytest.mark.parametrize("usage", ["parameter", "output", "relation"])
def test_referenced_entity_with_same_inline_selection_is_preserved(
    duplicate_target_intent, usage
):
    goal = duplicate_target_intent["goals"][0]
    value = {"kind": "entity_ref", "ref": "contacts"}
    if usage == "parameter":
        goal["parameters"].append({"name": "source", "value": value})
    elif usage == "output":
        goal["outputs"] = [{"entity_ref": "contacts", "role": "selected_contacts"}]
    else:
        duplicate_target_intent["relations"] = [
            {"type": "about_object", "arguments": [{"name": "object", "value": value}]}
        ]
    checked(duplicate_target_intent)


@pytest.mark.parametrize(
    "difference",
    ["condition", "owner", "quantifier", "time", "json_type", "array_order"],
)
def test_nonidentical_selection_is_not_rewritten_or_rejected(
    duplicate_target_intent, difference
):
    selection = duplicate_target_intent["goals"][0]["target"]["selection"]
    if difference == "condition":
        selection["condition"]["value"]["value"] = 25
    elif difference == "owner":
        selection["owner"] = "self"
    elif difference == "quantifier":
        selection["quantifier"] = {"mode": "any_one"}
    elif difference == "time":
        selection["state_anchor"] = {"kind": "user_request"}
    elif difference == "json_type":
        duplicate_target_intent["entities"][0]["selection"]["condition"]["value"][
            "value"
        ] = 0
        selection["condition"]["value"]["value"] = False
    else:
        left = duplicate_target_intent["entities"][0]["selection"]
        first = deepcopy(left["condition"])
        second = deepcopy(first)
        second["field"] = "years_in_address_book"
        left["condition"] = {"kind": "all", "items": [first, second]}
        selection["condition"] = {
            "kind": "all",
            "items": [deepcopy(second), deepcopy(first)],
        }
    original = deepcopy(duplicate_target_intent)
    checked(duplicate_target_intent)
    assert duplicate_target_intent == original


def test_duplicate_target_check_ignores_object_key_order(duplicate_target_intent):
    selection = duplicate_target_intent["goals"][0]["target"]["selection"]
    duplicate_target_intent["goals"][0]["target"]["selection"] = dict(
        reversed(list(selection.items()))
    )
    with pytest.raises(ValueError, match="Unused entity"):
        checked(duplicate_target_intent)


@pytest.mark.parametrize("branch", ["then_goals", "else_goals"])
def test_duplicate_target_check_covers_rule_goals(duplicate_target_intent, branch):
    goal = duplicate_target_intent["goals"].pop()
    duplicate_target_intent["rules"] = [
        {
            "id": "rule",
            "condition": {"kind": "predicate", "name": "approved", "arguments": []},
            "then_goals": [],
            branch: [goal],
        }
    ]
    with pytest.raises(ValueError, match=f"/rules/0/{branch}/0/target/selection"):
        checked(duplicate_target_intent)


def test_counting_rejects_incompatible_validation_profile():
    doc = {"goals": [{"id": "g", "action": "purchase"}]}
    with pytest.raises(ValueError, match="GAIA2 validation contract"):
        build_items(doc, PROFILE, ValidationProfile(nodes=VALIDATION.nodes))


def test_group_relation_binds_multiple_typed_goals():
    doc = {
        "goals": [{"id": name, "action": "custom_action"} for name in ["a", "b", "c"]],
        "relations": [
            {
                "type": "requires_all_completed",
                "arguments": [
                    {
                        "name": "prerequisites",
                        "value": {
                            "kind": "list",
                            "items": [
                                {"kind": "goal_ref", "ref": "a"},
                                {"kind": "goal_ref", "ref": "b"},
                            ],
                        },
                    },
                    {"name": "dependent", "value": {"kind": "goal_ref", "ref": "c"}},
                ],
            }
        ],
    }
    checked(doc)
    relation = next(
        (u for u in build_items(doc, PROFILE) if u["unit_type"] == "relation")
    )
    assert len(relation["references"]) == 3
    assert len(relation["source_paths"]) == 1


def test_removed_result_fields_are_rejected():
    with pytest.raises(ValidationError):
        checked({"goals": [{"id": "g", "action": "create", "result_ref": "x"}]})
    with pytest.raises(ValidationError):
        checked(
            {
                "entities": [
                    {"id": "x", "entity_type": "event", "created_by_goal": "g"}
                ],
                "goals": [{"id": "g", "action": "create"}],
            }
        )


@pytest.mark.parametrize(
    "quantifier",
    [
        {"mode": "all"},
        {"mode": "any_one"},
        {"mode": "exactly", "count": 0},
        {"mode": "exactly", "count": 3},
        {"mode": "at_least", "count": 2},
        {"mode": "at_most", "count": 5},
        {"mode": "range", "min_count": 2, "max_count": 4},
        {"mode": "exactly", "count": {"kind": "undetermined"}},
    ],
)
def test_quantifier_is_one_fact(quantifier):
    doc = selected(quantifier)
    if quantifier.get("count") == {"kind": "undetermined"}:
        doc["ambiguities"] = [
            {
                "path": "/goals/0/target/selection/quantifier/count",
                "reason": "Ambiguous count",
            }
        ]
    checked(doc)
    units = [u for u in build_items(doc, PROFILE) if u["unit_type"] == "quantifier"]
    assert len(units) == 1
    assert units[0]["values"] == [quantifier]


@pytest.mark.parametrize(
    "quantifier",
    [
        "one",
        {"mode": "unknown"},
        {"mode": "exactly"},
        {"mode": "all", "count": 1},
        {"mode": "any_one", "count": None},
        {"mode": "exactly", "count": -1},
        {"mode": "exactly", "count": True},
        {"mode": "exactly", "count": 1.5},
        {"mode": "range", "min_count": 3},
        {"mode": "range", "min_count": 4, "max_count": 2},
        {"mode": "range", "min_count": 4.0, "max_count": 2.0},
    ],
)
def test_invalid_quantifiers(quantifier):
    with pytest.raises((ValidationError, ValueError)):
        checked(selected(quantifier))


def test_dynamic_counts_are_references_not_guessed_values():
    doc = selected(
        {
            "mode": "range",
            "min_count": 0,
            "max_count": {
                "kind": "entity_field",
                "entity_ref": "attendees",
                "field": "count",
            },
        }
    )
    doc["entities"] = [{"id": "attendees", "entity_type": "person_group"}]
    checked(doc)
    quantifier = next(
        (u for u in build_items(doc, PROFILE) if u["unit_type"] == "quantifier")
    )
    assert quantifier["references"][0]["target_path"] == "/entities/0"
    bad = deepcopy(doc)
    bad["goals"][0]["target"]["selection"]["quantifier"]["max_count"]["entity_ref"] = (
        "g"
    )
    with pytest.raises(ValueError, match="Invalid entity reference"):
        checked(bad)


def rule_doc():
    return {
        "goals": [],
        "entities": [{"id": "dinner", "entity_type": "event"}],
        "rules": [
            {
                "id": "r",
                "trigger": {"event_type": "reply_received", "bind_as": "reply"},
                "condition": {"kind": "predicate", "name": "accepted", "arguments": []},
                "then_goals": [{"id": "g", "action": "select_objects"}],
            }
        ],
    }


@pytest.mark.parametrize(
    "anchor",
    [
        {"kind": "user_request"},
        {"kind": "rule_trigger", "rule_ref": "r"},
        {"kind": "goal_completion", "goal_ref": "g"},
        {"kind": "entity_time", "entity_ref": "dinner", "field": "start_datetime"},
        {"kind": "absolute_time", "value": "2026-10-12"},
        {"kind": "absolute_time", "value": "2026-10-12T09:30"},
        {"kind": "absolute_time", "value": "2026-10-12T09:30:15+08:00"},
        {"kind": "undetermined"},
    ],
)
def test_shared_temporal_anchor_locations(anchor):
    doc = rule_doc()
    goal = doc["rules"][0]["then_goals"][0]
    goal["target"] = {
        "kind": "selection",
        "selection": {"entity_type": "event", "state_anchor": anchor},
    }
    goal["parameters"] = [
        {
            "name": "date",
            "value": {
                "kind": "relative_date",
                "expression": "tomorrow",
                "anchor": anchor,
            },
        }
    ]
    doc["time_constraints"] = [
        {
            "scope_goal_ids": ["g"],
            "relation": "exactly_before",
            "anchor": anchor,
            "amount": 30,
            "unit": "minute",
        }
    ]
    checked(doc)
    units = build_items(doc, PROFILE)
    assert (
        sum(
            (
                u["source_paths"]
                == ["/rules/0/then_goals/0/target/selection/state_anchor"]
                for u in units
            )
        )
        == 1
    )
    assert not any(
        (p.endswith("/anchor/kind") for u in units for p in u["source_paths"])
    )


@pytest.mark.parametrize(
    "kind",
    [
        "policy",
        "arbitrary_business_category",
        "absolute_time",
        "rule_trigger",
        "comparison",
    ],
)
def test_business_constraint_kinds_do_not_act_as_type_tags(kind):
    doc = {
        "goals": [{"id": "g", "action": "query"}],
        "constraints": [
            {
                "kind": kind,
                "name": "custom_limit",
                "scope_goal_ids": ["g"],
                "value": {"kind": "literal", "value": 2},
            }
        ],
    }
    checked(doc)


def test_anchor_and_local_trigger_errors():
    doc = rule_doc()
    doc["time_constraints"] = [
        {
            "scope_goal_ids": ["g"],
            "relation": "after",
            "anchor": {"kind": "rule_trigger", "rule_ref": "r"},
        }
    ]
    doc["rules"][0].pop("trigger")
    with pytest.raises(ValueError, match="declared trigger"):
        checked(doc)
    doc = rule_doc()
    doc["goals"] = [
        {
            "id": "outside",
            "action": "purchase",
            "target": {
                "kind": "selection",
                "selection": {
                    "entity_type": "product",
                    "quantifier": {
                        "mode": "exactly",
                        "count": {
                            "kind": "trigger_field",
                            "event_ref": "reply",
                            "field": "count",
                        },
                    },
                },
            },
        }
    ]
    with pytest.raises(ValueError, match="Invalid trigger reference"):
        checked(doc)
    doc["rules"][0]["then_goals"] = doc.pop("goals")
    doc["goals"] = []
    checked(doc)


@pytest.mark.parametrize(
    "text", ["2026-02-30", "2026-10-12T25:00", "2026-10-12T09:30+25:00"]
)
def test_invalid_absolute_dates(text):
    doc = {
        "goals": [
            {
                "id": "g",
                "action": "schedule",
                "parameters": [
                    {
                        "name": "date",
                        "value": {
                            "kind": "relative_date",
                            "expression": "day_after",
                            "anchor": {"kind": "absolute_time", "value": text},
                        },
                    }
                ],
            }
        ]
    }
    with pytest.raises((ValidationError, ValueError)):
        checked(doc)


@pytest.mark.parametrize("value", ["09:30", "09:30:15"])
def test_seconds_are_preserved(value):
    doc = {
        "goals": [
            {
                "id": "g",
                "action": "schedule",
                "parameters": [
                    {"name": "time", "value": {"kind": "time_of_day", "value": value}}
                ],
            }
        ]
    }
    checked(doc)
    assert build_items(doc, PROFILE)[1]["values"][0]["value"]["value"] == value


def test_invalid_comparison_and_outputs():
    doc = selected({"mode": "all"})
    doc["goals"][0]["target"]["selection"]["condition"] = {
        "kind": "comparison",
        "field": "name",
        "operator": "contains",
        "value": {"kind": "literal", "value": "abc"},
    }
    with pytest.raises(ValidationError):
        checked(doc)
    doc = {
        "entities": [{"id": "e", "entity_type": "object"}],
        "goals": [
            {
                "id": "g",
                "action": "derive",
                "outputs": [{"entity_ref": "e", "role": "r"}],
            }
        ],
    }
    doc["goals"][0]["outputs"] *= 2
    with pytest.raises((ValidationError, ValueError)):
        checked(doc)
    doc["goals"][0]["outputs"] = [{"entity_ref": "g", "role": "r"}]
    with pytest.raises(ValueError, match="Invalid entity reference"):
        checked(doc)

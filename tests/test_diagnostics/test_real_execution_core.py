"""Behavioral tests for isolated scoring, matching ambiguity, and evidence gaps."""

import copy
import itertools
import random

import pytest

from tau2.diagnostics.execution.checks import (
    canonical_tool,
    compare_args,
    decode,
    hard_check,
)
from tau2.diagnostics.execution.matching import assignment
from tau2.diagnostics.execution.scoring import (
    aggregate,
    dependency_edges,
    dimension,
    score_trial,
)


def event(
    identity,
    target,
    time=None,
    parents=None,
    error=None,
    tool="RentAFlat__save_apartment",
):
    (app, function) = tool.split("__")
    return {
        "event_id": identity,
        "event_type": "AGENT",
        "event_time": time,
        "dependencies": parents or [],
        "action": {
            "app": app,
            "function": function,
            "operation_type": "WRITE",
            "args": {"apartment_id": target},
        },
        "metadata": {
            "exception": error,
            "return_value": None,
            "return_value_type": None,
        },
    }


def evidence(reference, actual, observation=None):
    scenario = {
        "events": reference,
        "apps": [{"name": "RentAFlat", "app_state": {"saved_apartments": []}}],
    }
    trace = {"events": copy.deepcopy(reference), "completed_events": actual}
    calls = [
        {
            "tool": e["action"]["app"] + "__" + e["action"]["function"],
            "args": copy.deepcopy(e["action"]["args"]),
        }
        for e in actual
    ]
    observation = observation or {
        "input_complete": True,
        "projection": {"complete": True, "deliveries": []},
    }
    official = {
        "status": "completed",
        "success": False,
        "observed_app_calls": len(calls),
    }
    return (scenario, trace, calls, observation, official)


def score(reference, actual):
    return score_trial(*evidence(reference, actual))


def test_hungarian_matches_bruteforce_for_rectangular_inputs():
    rng = random.Random(20261017)
    for rows in range(1, 5):
        for columns in range(1, 5):
            for _ in range(6):
                weights = [
                    [rng.randrange(5) for _ in range(columns)] for _ in range(rows)
                ]
                size = max(rows, columns)
                brute = max(
                    (
                        sum((weights[i][p[i]] for i in range(rows) if p[i] < columns))
                        for p in itertools.permutations(range(size))
                    )
                )
                (value, pairs) = assignment(weights)
                assert value == brute
                assert len(pairs) == min(rows, columns)
                assert len({i for (i, _) in pairs}) == len(pairs)
                assert len({j for (_, j) in pairs}) == len(pairs)
    assert assignment([]) == (0, [])


def test_typed_args_preserve_strings_and_decode_serialized_lists():
    assert decode("00123", "str") == "00123"
    assert decode('["a", "b"]', "list[str]") == ["a", "b"]
    assert decode("", "Optional[str]") is None
    assert decode("32", "Optional[int]") == 32
    assert decode("['x']", "list") == ["x"]


@pytest.mark.parametrize(
    "checker,actual,expected,result",
    [
        ("eq_checker", "a", "b", False),
        ("eq_str_strip_checker", " Eva \n", "Eva", True),
        ("phone_number_checker", "+420-123 456", "+420123456", True),
        ("phone_number_checker", "123", "124", False),
        ("unordered_list_checker", ["b", "a"], ["a", "b"], True),
        ("unordered_list_checker", None, [], True),
        ("unordered_path_list_checker", None, [], False),
        ("path_checker", "/x/../y", "y", True),
        ("datetime_checker", "2024-10-17 08:00:00", "2024-10-16 08:00:00", False),
        ("eq_checker", "actual", "{{created.id}}", None),
        ("unknown_checker", "a", "a", None),
    ],
)
def test_original_hard_checker_semantics(checker, actual, expected, result):
    assert hard_check(checker, actual, expected) is result


def test_attendee_tolerance_does_not_guess_missing_user_identity():
    assert hard_check("list_attendees_checker", ["Ada", "Self"], ["Ada"]) is None
    assert (
        hard_check("list_attendees_checker", ["Ada", "Self"], ["Ada"], "Self") is True
    )
    assert hard_check("list_attendees_checker", ["Other"], []) is True


def test_aliases_and_no_fuzzy_parameter_credit():
    assert canonical_tool("Emails", "send_email") == canonical_tool(
        "EmailClientV2", "send_email"
    )
    expected = {
        "first_name": "Eva",
        "last_name": "Malá",
        "email": "eva@x",
        "phone": "123",
    }
    actual = expected | {"phone": "124"}
    checks = compare_args(
        canonical_tool("Contacts", "add_new_contact"), actual, expected
    )
    assert [c["passed"] for c in checks] == [True, True, True, False]
    assert dimension(checks)["score"] == 0.75


def test_wrong_parameters_and_failed_call_still_get_tool_selection_credit():
    result = score([event("ref", "a")], [event("attempt", "b", 1, error="ValueError")])
    assert result["dimensions"]["tools"]["score"] == 1
    assert result["dimensions"]["parameters"]["score"] == 0
    assert result["dimensions"]["effects"]["score"] == 0
    assert result["total"]["score"] == pytest.approx(100 * 25 / 90)


def test_omission_and_duplicate_calls_affect_tool_precision_recall():
    missing = score([event("r1", "a"), event("r2", "b")], [event("a1", "a", 1)])
    tools = missing["dimensions"]["tools"]
    assert tools["precision"] == 1
    assert tools["recall"] == 0.5
    assert tools["score"] == pytest.approx(2 / 3)
    parameters = missing["dimensions"]["parameters"]
    assert parameters["score"] == 0.5
    assert parameters["reference_check_count"] == 2
    assert parameters["matched_only_diagnostic"]["score"] == 1
    assert parameters["units"][1]["reason"] == "required_operation_not_performed"
    extra = score([event("r", "a")], [event("a1", "a", 1), event("a2", "a", 2)])
    assert extra["dimensions"]["tools"]["precision"] == 0.5
    assert extra["dimensions"]["tools"]["recall"] == 1
    assert extra["dimensions"]["parameters"]["score"] == 1


def test_order_uses_dependencies_and_does_not_restrict_tool_matching():
    reference = [event("ra", "a"), event("rb", "b", parents=["ra"])]
    result = score(reference, [event("ab", "b", 1), event("aa", "a", 2)])
    assert result["dimensions"]["tools"]["score"] == 1
    assert result["dimensions"]["parameters"]["score"] == 1
    assert result["dimensions"]["order"]["score"] == 0
    assert result["total"]["score"] == pytest.approx(90)
    reference[1]["dependencies"] = []
    independent = score(reference, [event("ab", "b", 1), event("aa", "a", 2)])
    assert independent["dimensions"]["order"]["status"] == "not_applicable"
    assert independent["total"]["score"] == 100


def test_missing_dependency_endpoint_is_not_penalized_twice():
    result = score(
        [event("ra", "a"), event("rb", "b", parents=["ra"])], [event("ab", "b", 1)]
    )
    order = result["dimensions"]["order"]
    assert order["status"] == "not_applicable"
    assert order["reference_edges"] == 1
    assert order["edge_coverage"] == 0
    assert result["dimensions"]["parameters"]["matched_operation_coverage"] == 0.5


def test_equal_quality_pairing_cannot_choose_convenient_order():
    reference = [event("r1", "same"), event("r2", "same", parents=["r1"])]
    result = score(reference, [event("a1", "same", 1), event("a2", "same", 2)])
    assert all((len(x) == 2 for x in result["matching"]["alternatives"].values()))
    assert result["dimensions"]["parameters"]["score"] == 1
    assert result["dimensions"]["order"]["score"] is None
    assert result["dimensions"]["order"]["coverage"] == 0


def test_equal_timestamps_are_unknown_not_arbitrary_index_order():
    result = score(
        [event("r1", "a"), event("r2", "b", parents=["r1"])],
        [event("a1", "a", 1), event("a2", "b", 1)],
    )
    assert result["dimensions"]["order"]["score"] is None


def test_dependencies_project_through_excluded_read_nodes():
    helper = {"event_id": "read", "dependencies": ["a"]}
    assert dependency_edges(
        [event("a", "x"), helper, event("b", "y", parents=["read"])], {"a", "b"}
    ) == [("a", "b")]
    with pytest.raises(ValueError, match="cycle"):
        dependency_edges(
            [event("a", "x", parents=["b"]), event("b", "x", parents=["a"])], {"a", "b"}
        )
    with pytest.raises(ValueError, match="Missing reference dependency"):
        dependency_edges([event("a", "x", parents=["unknown"])], {"a"})


def test_effect_uses_state_transition_including_later_undo():
    result = score(
        [event("r", "a")],
        [
            event("save", "a", 1),
            event("undo", "a", 2, tool="RentAFlat__remove_saved_apartment"),
        ],
    )
    assert result["dimensions"]["parameters"]["score"] == 1
    assert result["dimensions"]["effects"]["score"] == 0


def test_staged_reference_cannot_be_judged_only_by_final_saved_state():
    reference = [
        event("save", "a"),
        event(
            "remove", "a", parents=["save"], tool="RentAFlat__remove_saved_apartment"
        ),
    ]
    actual = [
        event("actual-save", "a", 1),
        event("actual-remove", "a", 2, tool="RentAFlat__remove_saved_apartment"),
    ]
    result = score(reference, actual)
    assert result["dimensions"]["order"]["score"] == 1
    assert result["dimensions"]["effects"]["score"] is None
    assert result["total"]["upper"] == pytest.approx(100)


def test_soft_checks_preserve_unknown_weight_and_exact_fast_path():
    expected = {
        "recipients": ["a@x"],
        "cc": [],
        "attachment_paths": [],
        "subject": "Meeting",
        "content": "Meet on Tuesday",
    }
    actual = expected | {"content": "See you on Tuesday"}
    checks = compare_args(canonical_tool("Emails", "send_email"), actual, expected)
    result = dimension(checks)
    assert result["score"] is None
    assert result["lower"] == 0.75
    assert result["upper"] == 1
    assert result["units"][-1]["passed"] is None
    assert (
        dimension(
            compare_args(canonical_tool("Emails", "send_email"), expected, expected)
        )["score"]
        == 1
    )


def test_creation_receipt_does_not_automatically_verify_content():
    ref = event("ref", "unused", tool="Emails__send_email")
    ref["action"]["args"] = {
        "recipients": ["a@x"],
        "cc": [],
        "attachment_paths": [],
        "subject": "Hi",
        "content": "Tuesday",
    }
    actual = copy.deepcopy(ref)
    actual.update(event_id="actual", event_time=1)
    actual["action"]["args"]["content"] = "Next Tuesday"
    actual["metadata"]["return_value"] = "new-mail-id"
    actual["metadata"]["return_value_type"] = "str"
    result = score([ref], [actual])
    assert result["dimensions"]["tools"]["score"] == 1
    assert result["dimensions"]["effects"]["score"] is None
    actual["action"]["args"]["content"] = "Tuesday"
    assert score([ref], [actual])["dimensions"]["effects"]["score"] == 1


def test_unknown_evidence_is_not_dropped_like_structural_na():
    result = aggregate(
        {
            "tools": dimension([{"passed": True}]),
            "parameters": dimension([{"passed": None}]),
            "order": dimension([]),
            "effects": dimension([{"passed": False}]),
        }
    )
    assert result["score"] is None
    assert result["lower"] == pytest.approx(100 * 25 / 90)
    assert result["upper"] == pytest.approx(100 * 60 / 90)
    assert result["effective_weights"]["parameters"] == pytest.approx(35 / 90)


def test_no_calls_are_not_perfect_parameters_or_order():
    result = score([event("r", "a")], [])
    assert result["dimensions"]["tools"]["score"] == 0
    assert result["dimensions"]["parameters"]["score"] == 0
    assert result["dimensions"]["effects"]["score"] == 0
    assert result["total"]["score"] == 0


def test_parameters_count_all_reference_fields_and_ignore_unrequested_functions():
    contact = event("contact", "unused", tool="Contacts__add_new_contact")
    contact["action"]["args"] = {
        "first_name": "Ada",
        "last_name": "L",
        "email": "ada@x",
        "phone": "123",
    }
    unrelated = copy.deepcopy(contact)
    unrelated.update(event_id="unrequested", event_time=2)
    result = score([event("save", "a"), contact], [event("saved", "a", 1)])
    params = result["dimensions"]["parameters"]
    assert params["reference_check_count"] == 5
    assert params["score"] == 0.2
    assert params["coverage"] == 1
    extra = score([event("save", "a")], [unrelated])
    assert extra["dimensions"]["parameters"]["score"] == 0
    assert extra["dimensions"]["parameters"]["reference_check_count"] == 1


def test_missing_semantic_operation_scores_zero_without_a_judge():
    reference = event("mail", "unused", tool="Emails__send_email")
    reference["action"]["args"] = {
        "recipients": ["a@x"],
        "cc": [],
        "attachment_paths": [],
        "subject": "Meeting",
        "content": "Tuesday",
    }
    params = score([reference], [])["dimensions"]["parameters"]
    assert params["reference_check_count"] == 4
    assert params["score"] == 0
    assert params["units"][-1]["parameter"] == "$semantic_group"


def test_ambiguous_omission_keeps_all_reference_checks_and_unknowns():
    result = score([event("r1", "a"), event("r2", "a")], [event("a1", "a", 1)])
    params = result["dimensions"]["parameters"]
    assert params["reference_check_count"] == 2
    assert params["score"] is None
    assert params["lower"] == 0
    assert params["upper"] == 1


def test_audio_gap_does_not_erase_reliable_tool_evidence_but_transport_fault_does():
    args = list(evidence([event("r", "a")], [event("a", "a", 1)]))
    args[3]["projection"]["complete"] = False
    args[4]["status"] = "output_evidence_incomplete"
    assert score_trial(*args)["total"]["score"] == 100
    args[3]["error"] = "clock_discontinuity"
    result = score_trial(*args)
    assert result["diagnostic_only"]
    assert result["total"]["score"] is None
    assert result["total"]["lower"] is None


def test_query_has_no_tool_reference_and_does_not_ignore_last_reply():
    ref = event("reply", "unused", tool="AgentUserInterface__send_message_to_user")
    ref["action"]["args"] = {"content": "32"}
    args = list(evidence([ref], []))
    args[3]["projection"]["deliveries"] = [{"transcript": "32"}, {"transcript": "33"}]
    result = score_trial(*args)
    assert result["dimensions"]["tools"]["status"] == "not_applicable"
    assert result["total"]["score"] == 0
    args[3]["projection"]["deliveries"] = [{"transcript": "32.0"}]
    assert score_trial(*args)["total"]["score"] == 100
    args[3]["projection"]["complete"] = False
    assert score_trial(*args)["total"]["score"] is None


def test_evidence_mismatch_is_rejected():
    args = list(evidence([event("r", "a")], [event("a", "a", 1)]))
    args[2][0]["args"]["apartment_id"] = "tampered"
    with pytest.raises(ValueError, match="arguments disagree"):
        score_trial(*args)


def test_frozen_reference_cannot_be_changed_after_run():
    args = list(evidence([event("r", "a")], [event("a", "a", 1)]))
    args[0]["events"][0]["action"]["args"]["apartment_id"] = "different-reference"
    with pytest.raises(ValueError, match="reference disagree"):
        score_trial(*args)


def test_dynamic_scenarios_are_not_silently_scored_as_static():
    args = list(evidence([event("r", "a")], [event("a", "a", 1)]))
    environment = {"event_id": "env", "event_type": "ENV"}
    args[0]["events"].append(environment)
    args[1]["events"].append(copy.deepcopy(environment))
    with pytest.raises(ValueError, match="static single-request"):
        score_trial(*args)


def test_final_exported_apps_cannot_fake_a_successful_save():
    args = list(evidence([event("r", "a")], []))
    args[1]["apps"] = [{"name": "RentAFlat", "app_state": {"saved_apartments": ["a"]}}]
    assert score_trial(*args)["dimensions"]["effects"]["score"] == 0


def test_query_tasks_temporarily_use_only_binary_business_result():
    reply = event("reply", "unused", tool="AgentUserInterface__send_message_to_user")
    reply["action"]["args"] = {"content": "32"}
    args = list(evidence([reply], [event("extra-save", "unrequested", 1)]))
    args[3]["projection"]["deliveries"] = [{"transcript": "32"}]
    result = score_trial(*args)
    assert result["dimensions"]["tools"]["status"] == "not_applicable"
    assert result["dimensions"]["effects"]["score"] == 1
    assert result["total"]["score"] == 100


def test_previously_ungraded_address_enters_full_reference_denominator():
    reference = event("r", "unused", tool="Contacts__add_new_contact")
    reference["action"]["args"] = {
        "first_name": "Ada",
        "last_name": "L",
        "email": "ada@x",
        "phone": "123",
        "address": "Specified address",
    }
    result = score([reference], [])
    assert result["dimensions"]["parameters"]["unscored_reference_fields"] == []
    assert result["dimensions"]["parameters"]["reference_check_count"] == 5
    assert result["dimensions"]["parameters"]["score"] == 0


def test_wrong_address_fails_extended_parameter_check_and_receipt_effect():
    reference = event("ref", "unused", tool="Contacts__add_new_contact")
    reference["action"]["args"] = {
        "first_name": "Ada",
        "last_name": "L",
        "email": "ada@x",
        "phone": "123",
        "address": "Required address",
    }
    created = copy.deepcopy(reference)
    created.update(event_id="created", event_time=1)
    created["action"]["args"]["address"] = "Wrong address"
    created["metadata"].update(return_value="new-id", return_value_type="str")
    result = score([reference], [created])
    assert result["dimensions"]["parameters"]["score"] == 0.8
    assert result["dimensions"]["effects"]["score"] is None
    assert (
        result["dimensions"]["effects"]["units"][0]["reason"]
        == "receipt_present_but_required_content_unverified"
    )


def test_creation_followed_by_mutation_requires_final_state_evidence():
    reference = event("r", "unused", tool="Contacts__add_new_contact")
    reference["action"]["args"] = {
        "first_name": "Ada",
        "last_name": "L",
        "email": "ada@x",
        "phone": "123",
    }
    created = copy.deepcopy(reference)
    created.update(event_id="created", event_time=1)
    created["metadata"].update(return_value="new-id", return_value_type="str")
    removed = event("removed", "unused", 2, tool="Contacts__delete_contact")
    removed["action"]["args"] = {"contact_id": "new-id"}
    result = score([reference], [created, removed])
    assert result["dimensions"]["effects"]["score"] is None
    assert (
        result["dimensions"]["effects"]["units"][0]["reason"]
        == "later_mutation_requires_final_state_evidence"
    )

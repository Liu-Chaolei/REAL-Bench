"""Pure evidence scoring. Unknown evidence retains its weight as an interval."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from . import VERSION
from .call_evidence import align_calls
from .checks import (
    POLICY,
    POLICY_SHA256,
    canonical_tool,
    compare_args,
    decode,
    event_args,
)
from .matching import match_operations

WEIGHTS = {"tools": 25, "parameters": 35, "order": 10, "effects": 30}
AUI = "AgentUserInterface__send_message_to_user"


def dimension(units: list[dict], reason: str = "") -> dict:
    if not units:
        return {
            "status": "not_applicable",
            "score": None,
            "lower": None,
            "upper": None,
            "coverage": None,
            "reason": reason,
            "units": [],
        }
    passed = sum(u["passed"] is True for u in units)
    known = sum(u["passed"] is not None for u in units)
    lower = passed / len(units)
    upper = (passed + len(units) - known) / len(units)
    return {
        "status": "measured" if known == len(units) else "evidence_incomplete",
        "score": lower if known == len(units) else None,
        "lower": lower,
        "upper": upper,
        "coverage": known / len(units),
        "reason": reason,
        "units": units,
    }


def operations(events: list, reference: bool) -> tuple[list, list]:
    result, warnings = [], []
    identities = set()
    for event in events:
        if event.get("event_type") != "AGENT" or not event.get("action"):
            continue
        action = event["action"]
        tool = canonical_tool(action.get("app", ""), action.get("function", ""))
        if tool == AUI or action.get("operation_type") == "READ":
            continue
        if action.get("operation_type") != "WRITE" and tool not in POLICY["tool_args"]:
            warnings.append("unknown_reference_operation_type:" + tool)
            continue
        identity = event.get("event_id")
        if not identity or identity in identities:
            raise ValueError("Operation event IDs must be present and unique")
        identities.add(identity)
        metadata = event.get("metadata") or {}
        result.append(
            {
                "id": identity,
                "tool": tool,
                "app": action.get("app"),
                "args": event_args(event),
                "time": event.get("event_time"),
                "succeeded": None
                if reference or "exception" not in metadata
                else metadata["exception"] is None,
                "return_value": decode(
                    metadata.get("return_value"), metadata.get("return_value_type")
                ),
            }
        )
    if not reference:
        result.sort(key=lambda e: e["time"] if finite_time(e["time"]) else math.inf)
    return result, warnings


def finite_time(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def dependency_edges(events: list, retained: set[str]) -> list[tuple[str, str]]:
    by_id = {e["event_id"]: e for e in events}
    if len(by_id) != len(events):
        raise ValueError("Duplicate reference event ID")

    def parents(identity, visiting):
        if identity in visiting:
            raise ValueError("Reference dependency cycle")
        if identity not in by_id:
            raise ValueError("Missing reference dependency: " + identity)
        found = set()
        for parent in by_id[identity].get("dependencies", []):
            if parent in retained:
                if parent == identity or parent in visiting:
                    raise ValueError("Reference dependency cycle")
                found.add(parent)
            else:
                found |= parents(parent, visiting | {identity})
        return found

    edges = {(p, child) for child in retained for p in parents(child, set())}
    # Check cycles even when every endpoint is retained.
    graph = {child: {p for p, c in edges if c == child} for child in retained}

    def visit(node, visiting, done):
        if node in visiting:
            raise ValueError("Reference dependency cycle")
        if node in done:
            return
        for parent in graph[node]:
            visit(parent, visiting | {node}, done)
        done.add(node)

    done = set()
    for node in retained:
        visit(node, set(), done)
    return sorted(edges)


def tool_score(reference: list, actual: list, matching: dict) -> dict:
    expected, observed, matched = len(reference), len(actual), len(matching["pairs"])
    if expected == observed == 0:
        return dimension([], "no_reference_business_tool_path")
    precision = matched / observed if observed else 1.0
    recall = matched / expected if expected else 1.0
    f1 = 2 * matched / (expected + observed)
    return {
        "status": "measured",
        "score": f1,
        "lower": f1,
        "upper": f1,
        "coverage": 1.0,
        "precision": precision,
        "recall": recall,
        "matched": matched,
        "reference_count": expected,
        "actual_count": observed,
        "includes_failed_attempts": True,
        "units": [],
    }


def topological_operations(reference: list, edges: list) -> list:
    pending = list(reference)
    done, ordered = set(), []
    while pending:
        ready = next(
            (
                row
                for row in pending
                if all(parent in done for parent, child in edges if child == row["id"])
            ),
            None,
        )
        if ready is None:
            raise ValueError("Reference dependency cycle or missing operation")
        ordered.append(ready)
        done.add(ready["id"])
        pending.remove(ready)
    return ordered


def parameter_score(
    reference: list, actual: list, matching: dict, user_name=""
) -> dict:
    """All required parameter checks form the denominator, including omissions."""
    units, matched_units = [], []
    for i, event in enumerate(reference):
        j = matching["pairs"].get(i)
        if j is not None:
            checks = matching["checks"][i]
        else:
            checks = compare_args(event["tool"], {}, event["args"], user_name)
            missing = matching["alternatives"].get(i, [None]) == [None]
            checks = [
                {
                    **check,
                    "passed": False if missing else None,
                    "reason": "required_operation_not_performed"
                    if missing
                    else "ambiguous_optimal_matching",
                }
                if check["reason"]
                not in {"unsupported_tool", "missing_reference_parameter"}
                else check
                for check in checks
            ]
        rows = [
            {
                "reference_id": event["id"],
                "actual_id": actual[j]["id"] if j is not None else None,
                **check,
            }
            for check in checks
        ]
        units.extend(rows)
        if j is not None:
            matched_units.extend(rows)
    result = dimension(units, "no_reference_parameter_checks" if not units else "")
    result.update(
        denominator="all_reference_parameter_checks",
        reference_check_count=len(units),
        unmatched_operation_count=len(reference) - len(matching["pairs"]),
        matched_only_diagnostic=dimension(
            matched_units, "no_matched_operations" if not matched_units else ""
        ),
    )
    return result


def order_score(reference: list, actual: list, edges: list, matching: dict) -> dict:
    index = {e["id"]: i for i, e in enumerate(reference)}
    units, missing = [], []
    for parent, child in edges:
        left = matching["alternatives"].get(index[parent], [None])
        right = matching["alternatives"].get(index[child], [None])
        if left == [None] or right == [None]:
            missing.append([parent, child])
            continue
        results = set()
        for i in left:
            for j in right:
                a = actual[i]["time"] if i is not None else None
                b = actual[j]["time"] if j is not None else None
                results.add(
                    a < b if finite_time(a) and finite_time(b) and a != b else None
                )
        passed = next(iter(results)) if len(results) == 1 else None
        units.append(
            {
                "parent": parent,
                "child": child,
                "passed": passed,
                "reason": "event_time_precedence"
                if passed is not None
                else "ambiguous_matching_or_time",
            }
        )
    score = dimension(units, "no_checkable_dependency_edges" if not units else "")
    score.update(
        reference_edges=len(edges),
        checkable_edges=len(units),
        edge_coverage=len(units) / len(edges) if edges else None,
        excluded_missing_endpoints=missing,
    )
    return score


def saved_state(scenario: dict, actual: list) -> tuple[set | None, str]:
    app = next(
        (
            a
            for a in scenario.get("apps", [])
            if canonical_tool(a.get("name", ""), "save_apartment")
            == "ApartmentListingApp__save_apartment"
        ),
        None,
    )
    initial = app.get("app_state", {}).get("saved_apartments") if app else None
    if not isinstance(initial, list):
        return None, "initial_saved_apartments_unavailable"
    state = set(initial)
    related = [e for e in actual if e["tool"].startswith("ApartmentListingApp__")]
    if any(not finite_time(e["time"]) for e in related) or len(
        {e["time"] for e in related}
    ) != len(related):
        return None, "saved_state_event_order_ambiguous"
    for event in related:
        if event["succeeded"] is False:
            continue
        if event["succeeded"] is None:
            return None, "operation_success_evidence_missing"
        target = event["args"].get("apartment_id")
        if not isinstance(target, str):
            return None, "saved_state_target_missing"
        if event["tool"].endswith("__save_apartment"):
            state.add(target)
        elif event["tool"].endswith("__remove_saved_apartment"):
            state.discard(target)
        else:
            return None, "unsupported_apartment_state_transition"
    return state, "reconstructed_from_initial_state_and_successful_writes"


def receipt_effect(index: int, reference: list, actual: list, matching: dict) -> dict:
    alternatives = matching["alternatives"].get(index, [None])
    representative = matching["pairs"].get(index)
    if alternatives == [None]:
        return {"passed": False, "reason": "required_creation_not_performed"}
    if representative is None or None in alternatives or len(alternatives) != 1:
        return {"passed": None, "reason": "ambiguous_creation_matching"}
    event = actual[representative]
    if not event["tool"].startswith("EmailClientApp__") and any(
        other["tool"].split("__")[0] == event["tool"].split("__")[0]
        and other["tool"] != event["tool"]
        and other["succeeded"] is not False
        and (
            not finite_time(other["time"])
            or not finite_time(event["time"])
            or other["time"] >= event["time"]
        )
        for other in actual
    ):
        return {
            "passed": None,
            "reason": "later_mutation_requires_final_state_evidence",
        }
    checks = matching["checks"][index]
    if event["succeeded"] is False or any(
        c["passed"] is False and c.get("critical") for c in checks
    ):
        return {
            "passed": False,
            "reason": "creation_failed_or_wrong_required_parameters",
        }
    receipt = event["return_value"]
    if (
        not isinstance(receipt, str)
        or not receipt.strip()
        or event["succeeded"] is not True
    ):
        return {"passed": None, "reason": "creation_receipt_unavailable"}
    if any(c["passed"] is not True for c in checks):
        return {
            "passed": None,
            "reason": "receipt_present_but_required_content_unverified",
            "receipt": receipt,
        }
    if any(
        key not in POLICY["tool_args"].get(event["tool"], {})
        and value not in (None, "", [], {})
        and event["args"].get(key) != value
        for key, value in reference[index]["args"].items()
    ):
        return {
            "passed": None,
            "reason": "receipt_does_not_verify_ungraded_reference_fields",
            "receipt": receipt,
        }
    return {
        "passed": True,
        "reason": "successful_creation_receipt_and_requirement_checks",
        "receipt": receipt,
    }


def query_effect(scenario: dict, observation: dict) -> dict:
    answers = [
        event_args(e).get("content")
        for e in scenario.get("events", [])
        if e.get("event_type") == "AGENT"
        and canonical_tool(
            e.get("action", {}).get("app", ""),
            e.get("action", {}).get("function", ""),
        )
        == AUI
    ]
    if len(answers) != 1 or not isinstance(answers[0], str):
        return dimension([], "query_reference_result_unavailable")
    expected = answers[0]
    projection = observation.get("projection", {})
    unit = {"kind": "query_business_result", "expected": expected, "passed": None}
    if not projection.get("complete"):
        unit["reason"] = "audible_result_evidence_incomplete"
    elif not projection.get("deliveries"):
        unit.update(passed=False, reason="no_delivered_result")
    else:
        actual = projection["deliveries"][-1].get("transcript")
        unit["actual"] = actual
        numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
        if not isinstance(actual, str):
            unit["reason"] = "result_transcript_unavailable"
        elif re.fullmatch(numeric, expected.strip()) and re.fullmatch(
            numeric, actual.strip()
        ):
            unit.update(
                passed=Decimal(expected.strip()) == Decimal(actual.strip()),
                reason="numeric_business_value",
            )
        elif actual.strip() == expected.strip():
            unit.update(passed=True, reason="exact_business_result")
        else:
            unit["reason"] = "requires_semantic_result_review"
    return dimension([unit])


def effect_score(
    scenario: dict, reference: list, actual: list, matching: dict, observation: dict
) -> dict:
    if not reference:
        return query_effect(scenario, observation)
    state, state_reason = saved_state(scenario, actual)
    units = []
    for index, event in enumerate(reference):
        unit = {"reference_id": event["id"], "tool": event["tool"], "passed": None}
        if event["tool"] in {
            "ApartmentListingApp__save_apartment",
            "ApartmentListingApp__remove_saved_apartment",
        }:
            target = event["args"].get("apartment_id")
            conflicting_phases = any(
                other["tool"]
                in {
                    "ApartmentListingApp__save_apartment",
                    "ApartmentListingApp__remove_saved_apartment",
                }
                and other["tool"] != event["tool"]
                and other["args"].get("apartment_id") == target
                for other in reference
            )
            if conflicting_phases:
                unit["reason"] = "staged_reference_effect_requires_phase_validator"
            elif state is None or not isinstance(target, str) or "{{" in target:
                unit["reason"] = state_reason if state is None else "unresolved_target"
            else:
                wanted = event["tool"].endswith("__save_apartment")
                unit.update(passed=(target in state) == wanted, reason=state_reason)
        elif event["tool"] in {
            "EmailClientApp__send_email",
            "ContactsApp__add_new_contact",
            "CalendarApp__add_calendar_event",
        }:
            unit.update(receipt_effect(index, reference, actual, matching))
        else:
            if matching["alternatives"].get(index, [None]) == [None]:
                unit.update(passed=False, reason="required_operation_not_performed")
            else:
                unit["reason"] = "effect_validator_not_implemented_for_tool"
        units.append(unit)
    return dimension(units)


def aggregate(dimensions: dict) -> dict:
    applicable = [k for k, d in dimensions.items() if d["status"] != "not_applicable"]
    weight = sum(WEIGHTS[k] for k in applicable)
    if not weight:
        return {
            "score": None,
            "lower": None,
            "upper": None,
            "coverage": 0.0,
            "effective_weights": {},
        }
    effective = {k: WEIGHTS[k] / weight for k in applicable}
    lower = 100 * sum(effective[k] * dimensions[k]["lower"] for k in applicable)
    upper = 100 * sum(effective[k] * dimensions[k]["upper"] for k in applicable)
    return {
        "score": lower if abs(upper - lower) < 1e-9 else None,
        "lower": lower,
        "upper": upper,
        "coverage": sum(effective[k] * dimensions[k]["coverage"] for k in applicable),
        "effective_weights": effective,
        "reason": "unknown_evidence_preserves_weight_as_interval",
    }


def score_trial(
    scenario: dict,
    trace: dict,
    calls: list,
    observation: dict,
    official: dict,
    *,
    comparator=None,
    effect_provider=None,
) -> dict:
    if "events" not in scenario or "completed_events" not in trace:
        raise ValueError("Reference events and recorded completed_events are required")

    def reference_signature(event):
        action = event.get("action") or {}
        return (
            event.get("event_type"),
            tuple(sorted(event.get("dependencies", []))),
            canonical_tool(action.get("app", ""), action.get("function", "")),
            event_args(event),
        )

    recorded_reference = {e["event_id"]: e for e in trace.get("events", [])}
    if len(recorded_reference) != len(trace.get("events", [])) or any(
        e["event_id"] not in recorded_reference
        or reference_signature(e)
        != reference_signature(recorded_reference[e["event_id"]])
        for e in scenario["events"]
    ):
        raise ValueError("Frozen scenario and recorded reference disagree")
    if sum(e.get("event_type") == "USER" for e in scenario["events"]) > 1 or any(
        e.get("event_type") in {"ENV", "CONDITION"}
        or e.get("absolute_event_time") is not None
        or e.get("event_time_comparator") is not None
        or float(e.get("event_relative_time") or 0) > 30
        for e in scenario["events"]
    ):
        raise ValueError("Only the current static single-request protocol is supported")
    reference, warnings = operations(scenario["events"], reference=True)
    actual, actual_warnings = operations(trace["completed_events"], reference=False)
    warnings += actual_warnings
    if warnings:
        raise ValueError(
            "Cannot establish operation classification: " + "; ".join(warnings)
        )
    call_evidence = align_calls(
        trace["completed_events"], calls, official.get("observed_app_calls")
    )
    context = scenario.get("context") or {}
    user = context.get("user_details", {}) if isinstance(context, dict) else {}
    user_name = " ".join(
        str(user.get(k, "")) for k in ("first_name", "last_name")
    ).strip()
    matching = match_operations(reference, actual, user_name, comparator=comparator)
    edges = dependency_edges(scenario["events"], {e["id"] for e in reference})
    dimensions = {
        "tools": tool_score(reference, actual, matching)
        if reference
        else dimension([], "query_uses_binary_business_result"),
        "parameters": parameter_score(reference, actual, matching, user_name),
        "order": order_score(reference, actual, edges, matching),
        "effects": (effect_provider or effect_score)(
            scenario, reference, actual, matching, observation
        ),
    }
    dimensions["parameters"]["matched_operation_coverage"] = (
        len(matching["pairs"]) / len(reference) if reference else None
    )
    dimensions["parameters"]["unscored_reference_fields"] = [
        {"reference_id": event["id"], "parameter": key}
        for event in reference
        for key, value in event["args"].items()
        if key not in POLICY["tool_args"].get(event["tool"], {})
        and value not in (None, "", [], {})
    ]
    total = aggregate(dimensions)
    invalid = official.get("status") not in {"completed", "output_evidence_incomplete"}
    invalid |= bool(observation.get("error")) or not observation.get("input_complete")
    if invalid:
        total.update(
            score=None,
            lower=None,
            upper=None,
            reason="invalid_observation_diagnostic_only",
        )
    return {
        "version": VERSION,
        "call_evidence": call_evidence,
        "checker_policy_sha256": POLICY_SHA256,
        "weights": WEIGHTS,
        "official_status": official.get("status"),
        "official_success": official.get("success"),
        "diagnostic_only": invalid,
        "dimensions": dimensions,
        "total": total,
        "reference_operations": reference,
        "actual_operations": actual,
        "matching": {
            "pairs": [
                {"reference_id": reference[i]["id"], "actual_id": actual[j]["id"]}
                for i, j in sorted(matching["pairs"].items())
            ],
            "alternatives": {
                reference[i]["id"]: [
                    actual[j]["id"] if j is not None else None for j in choices
                ]
                for i, choices in matching["alternatives"].items()
            },
            "objective": matching["objective"],
            "uses_order_or_effects": False,
        },
        "warnings": matching["warnings"]
        + ["exported_apps_not_assumed_to_be_final_state"],
        "network_calls": 0,
    }

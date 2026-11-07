"""Business-state assertions for Gaia2 tasks under tau2's reward composition."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from tau2.data_model.message import AssistantMessage
from tau2.domains.gaia2.tools import Gaia2Tools
from tau2.environment.scenario import ScenarioRuntime, resolve_references

_ID = re.compile(r"^[0-9a-f]{32}$")
_EPHEMERAL = {
    "timestamp",
    "is_read",
    "time_read",
    "last_updated",
    "start_strftime",
    "end_strftime",
    "quotation_history",
    "api_call_limit",
    "order_date",
}
_TEXT = {"subject", "content", "description", "title"}


def reference_execution(snapshot: list[dict], spec, assets: Path):
    """Derive a target by executing the source operations with a virtual clock.

    This is data preparation/reward evaluation, never an alternate agent runner.
    No model, user proxy, ARE Environment or ARE judge is constructed.
    """
    target = Gaia2Tools(snapshot, assets=assets)
    runtime = ScenarioRuntime(spec, target.execute)
    target.runtime = runtime
    for app in target.apps.values():
        app.register_time_manager(runtime)
    pending = list(spec.events)
    try:
        while pending:
            ready = [
                e
                for e in pending
                if all(p in runtime.completed for p in e.dependencies)
            ]
            if not ready:
                raise ValueError("Reference operations have unresolved dependencies")
            ready.sort(key=runtime.event_priority)
            event = ready[0]
            due = (
                max((runtime.completed[p] for p in event.dependencies), default=0)
                + event.delay_seconds
            )
            runtime._manual = max(runtime.elapsed, due)
            arguments = resolve_references(event.arguments, runtime.results)
            if (
                event.kind == "user"
                or event.tool == "AgentUserInterface__send_message_to_user"
            ):
                result = None
            else:
                result = target.execute(event.tool, arguments)
            runtime.results[event.id] = result
            runtime.completed[event.id] = runtime.elapsed
            pending.remove(event)
        target.refresh_db(include_files=True)
        return copy.deepcopy(target.db.apps), dict(runtime.results)
    finally:
        target.close()


def _initial_identifiers(value) -> set[str]:
    if isinstance(value, dict):
        result = {str(k) for k in value if _ID.fullmatch(str(k))}
        for child in value.values():
            result.update(_initial_identifiers(child))
        return result
    if isinstance(value, list):
        result = set()
        for child in value:
            result.update(_initial_identifiers(child))
        return result
    return {value} if isinstance(value, str) and _ID.fullmatch(value) else set()


def business_state(value, initial_ids: set[str]):
    """Represent generated identities as variables, retaining all foreign keys."""
    if isinstance(value, dict):
        pairs = []
        generated_keys = False
        for key, child in value.items():
            if key in _EPHEMERAL:
                continue
            key = str(key)
            if _ID.fullmatch(key) and key not in initial_ids:
                key = {"@generated_id": key}
                generated_keys = True
            pairs.append([key, business_state(child, initial_ids)])
        return {"@entities": pairs} if generated_keys else dict(pairs)
    if isinstance(value, list):
        return [business_state(child, initial_ids) for child in value]
    if isinstance(value, str) and _ID.fullmatch(value) and value not in initial_ids:
        return {"@generated_id": value}
    return value


def _equivalent(
    actual,
    expected,
    *,
    field="",
    scoring=None,
    differences=None,
    literal_texts=None,
    state_bindings=None,
):
    """Match entity graphs bijectively, backtracking across unordered collections.

    Semantic text checks never determine identity by themselves. One actual
    identifier must correspond to exactly one target identifier everywhere,
    including references from other applications. Backtracking permits harmless
    creation-order changes without losing these relationships.
    """
    import math

    from tau2.config import DEFAULT_SCENARIO_STATE_MATCH_LIMIT

    literals = literal_texts or set()
    cache = {}
    visits = 0

    def marker(value):
        return isinstance(value, dict) and set(value) == {"@generated_id"}

    def bind(left, right, mapping):
        if left in mapping:
            return mapping if mapping[left] == right else None
        if right in mapping.values():
            return None
        return {**mapping, left: right}

    def equal_identifiers(value, mapping):
        if marker(value):
            return bind(value["@generated_id"], value["@generated_id"], mapping)
        children = (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else []
        )
        for child in children:
            mapping = equal_identifiers(child, mapping)
            if mapping is None:
                break
        return mapping

    def has_identifiers(value):
        if marker(value):
            return True
        children = (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else []
        )
        return any(has_identifiers(child) for child in children)

    def solve(left, right, key, mapping, checks):
        nonlocal visits
        visits += 1
        if visits > DEFAULT_SCENARIO_STATE_MATCH_LIMIT:
            raise RuntimeError(
                "Business-state comparison exceeded its declared search limit"
            )
        if key in _TEXT and (
            (left is None and right == "") or (right is None and left == "")
        ):
            yield mapping, checks
            return
        if marker(left) or marker(right):
            if marker(left) and marker(right):
                updated = bind(left["@generated_id"], right["@generated_id"], mapping)
                if updated is not None:
                    yield updated, checks
            return
        if left == right:
            updated = equal_identifiers(left, mapping)
            if updated is not None:
                yield updated, checks
            if not has_identifiers(left):
                return
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return
            if set(right) == {"@entities"}:
                # A single generated key must not turn hundreds of original IDs
                # into a permutation problem. Keep ID/value pairs atomic.
                def partition(pairs):
                    fixed, generated = {}, []
                    for identity, value in pairs:
                        if marker(identity):
                            generated.append({"identity": identity, "entity": value})
                        else:
                            fixed[identity] = value
                    return fixed, generated

                left_fixed, left_new = partition(left["@entities"])
                right_fixed, right_new = partition(right["@entities"])
                if left_fixed.keys() != right_fixed.keys() or len(left_new) != len(
                    right_new
                ):
                    return
                for updated, rows in solve(
                    left_fixed, right_fixed, key, mapping, checks
                ):
                    yield from solve(left_new, right_new, key, updated, rows)
                return
            # Cheap structural/identity mismatches precede semantic text checks.
            keys = sorted(
                right,
                key=lambda name: (name in _TEXT, isinstance(right[name], (dict, list))),
            )
            failed_fields = set()

            def fields(index, current, selected):
                state = (index, tuple(sorted(current.items())))
                if state in failed_fields:
                    return
                if index == len(keys):
                    yield current, selected
                    return
                name = keys[index]
                matched = False
                for updated, rows in solve(
                    left[name], right[name], name, current, selected
                ):
                    for outcome in fields(index + 1, updated, rows):
                        matched = True
                        yield outcome
                if not matched:
                    failed_fields.add(state)

            yield from fields(0, mapping, checks)
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return
            failed_items = set()

            def items(index, available, current, selected):
                state = (index, available, tuple(sorted(current.items())))
                if state in failed_items:
                    return
                if index == len(right):
                    yield current, selected
                    return
                candidates = sorted(available, key=lambda i: left[i] != right[index])
                matched = False
                for candidate in candidates:
                    for updated, rows in solve(
                        left[candidate], right[index], key, current, selected
                    ):
                        for outcome in items(
                            index + 1,
                            tuple(i for i in available if i != candidate),
                            updated,
                            rows,
                        ):
                            matched = True
                            yield outcome
                if not matched:
                    failed_items.add(state)

            yield from items(0, tuple(range(len(left))), mapping, checks)
            return
        if (
            isinstance(left, (int, float))
            and isinstance(right, (int, float))
            and not isinstance(left, bool)
            and not isinstance(right, bool)
        ):
            if math.isclose(left, right, rel_tol=0, abs_tol=1e-9):
                yield mapping, checks
            return
        if isinstance(left, str) and isinstance(right, str) and key in _TEXT:
            if right in literals:
                return  # Explicitly requested labels are literal business data.
            if " ".join(left.split()).casefold() == " ".join(right.split()).casefold():
                yield mapping, checks
                return
            if scoring is not None and scoring.scorer.value == "semantic":
                from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

                pair = (left, right)
                if pair not in cache:
                    result = CommunicateEvaluator.evaluate_communicate_info(
                        [AssistantMessage(role="assistant", content=left)],
                        [right],
                        scoring=scoring,
                    )[0]
                    cache[pair] = {
                        "field": key,
                        "expected": right,
                        "actual": left,
                        "met": result.met,
                        "justification": result.justification,
                    }
                row = cache[pair]
                if row["met"]:
                    yield mapping, checks + [row]

    solution = next(solve(actual, expected, field, {}, []), None)
    if differences is not None:
        differences.extend(solution[1] if solution is not None else cache.values())
    if solution is not None and state_bindings is not None:
        state_bindings.update(solution[0])
    return solution is not None


def check_goal(tools: Gaia2Tools) -> bool:
    """Compare actual business outcomes with the frozen task's target outcomes."""
    spec = tools.runtime.spec
    target, reference_values = reference_execution(
        tools.source_snapshot, spec, tools.assets
    )
    identifiers = _initial_identifiers(tools.source_snapshot)
    tools.refresh_db(include_files=True)
    actual = business_state(
        _project_outcomes(tools.db.apps, tools.source_snapshot, spec), identifiers
    )
    expected = business_state(
        _project_outcomes(target, tools.source_snapshot, spec), identifiers
    )
    details = []
    state_bindings = {}
    met = _equivalent(
        actual,
        expected,
        scoring=tools.semantic_scoring,
        differences=details,
        literal_texts=_literal_labels(spec),
        state_bindings=state_bindings,
    )
    inverse = {expected: actual for actual, expected in state_bindings.items()}

    def actual_result(value):
        if isinstance(value, dict):
            return {key: actual_result(child) for key, child in value.items()}
        if isinstance(value, list):
            return [actual_result(child) for child in value]
        if isinstance(value, str):
            for expected, actual in inverse.items():
                value = value.replace(expected, actual)
        return value

    tools.reference_bindings = (
        {key: actual_result(value) for key, value in reference_values.items()}
        if met
        else {}
    )
    tools.goal_diagnostics = {
        "version": "gaia2-tau-v1",
        "state_match": met,
        "semantic_checks": details,
    }
    return met


def _project_outcomes(state, snapshot, spec):
    """Grade cab bookings by requested effects, rather than random quote draws."""
    state = copy.deepcopy(state)
    original = _initial_identifiers(snapshot)
    explicit_times = any(
        e.tool == "Cabs__order_ride" and e.arguments.get("ride_time")
        for e in spec.events
    )
    from tau2.domains.gaia2.tools import location_aliases, normalize_arguments

    aliases = location_aliases(snapshot)
    for ride in state.get("Cabs", {}).get("ride_history", []):
        if ride.get("ride_id") in original:
            continue
        for key in ("price", "duration", "delay", "delay_history", "distance_km"):
            ride.pop(key, None)
        if not explicit_times:
            ride.pop("time_stamp", None)
        for key in ("start_location", "end_location"):
            if key in ride:
                ride[key] = normalize_arguments(
                    "Cabs__order_ride", {key: ride[key]}, aliases
                )[key]
    return state


def _literal_labels(spec):
    """Keep explicitly quoted event titles and email subjects exact."""
    pattern = re.compile(
        r"\b(?:title|titled|name|named|called|subject)(?:\s+\w+){0,5}?\s*[, :\-]?\s*(?:[\"“]([^\"”]+)[\"”]|['‘]([^'’]+)['’])",
        re.IGNORECASE,
    )
    text = " ".join(
        str(e.arguments.get("content", "")) for e in spec.events if e.kind == "user"
    )
    return {match.group(1) or match.group(2) for match in pattern.finditer(text)}

"""Maximum-cardinality tool matching; order/outcomes never select a pairing."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .bindings import ReferenceBindings
from .checks import compare_args


def assignment(weights: list[list[int]]) -> tuple[int, list[tuple[int, int]]]:
    """Square-padded Hungarian assignment; supports rectangular and empty inputs."""
    rows = len(weights)
    cols = len(weights[0]) if rows else 0
    size = max(rows, cols)
    if not size:
        return 0, []
    maximum = max((x for row in weights for x in row), default=0)
    costs = [
        [maximum - (weights[i][j] if i < rows and j < cols else 0) for j in range(size)]
        for i in range(size)
    ]
    u, v, p, way = ([0] * (size + 1) for _ in range(4))
    for i in range(1, size + 1):
        p[0], j0 = i, 0
        minimum = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, size + 1):
                if not used[j]:
                    current = costs[i0 - 1][j - 1] - u[i0] - v[j]
                    if current < minimum[j]:
                        minimum[j], way[j] = current, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    pairs = [
        (p[j] - 1, j - 1) for j in range(1, size + 1) if p[j] <= rows and j <= cols
    ]
    return sum(weights[i][j] for i, j in pairs), pairs


def forced_score(weights: list[list[int]], i: int, j: int) -> int:
    remaining = [
        [x for column, x in enumerate(row) if column != j]
        for index, row in enumerate(weights)
        if index != i
    ]
    score, _ = assignment(remaining)
    return weights[i][j] + score


def _match_operations(
    reference: list,
    actual: list,
    user_name: str = "",
    comparator=None,
    eligibility=None,
) -> dict[str, Any]:
    groups = defaultdict(lambda: ([], []))
    for index, event in enumerate(reference):
        groups[
            (
                event["tool"],
                event.get("app") if event.get("app") in {"Messages", "Chats"} else None,
            )
        ][0].append(index)
    for index, event in enumerate(actual):
        groups[
            (
                event["tool"],
                event.get("app") if event.get("app") in {"Messages", "Chats"} else None,
            )
        ][1].append(index)
    pairs, alternatives, details = {}, {}, {}
    warnings = []
    for (tool, _), (left, right) in sorted(
        groups.items(), key=lambda item: str(item[0])
    ):
        if not left or not right:
            for i in left:
                alternatives[i] = [None]
            continue
        comparisons = {
            (i, j): comparator(reference[i], actual[j], user_name)
            if comparator is not None
            else compare_args(tool, actual[j]["args"], reference[i]["args"], user_name)
            for i in left
            for j in right
        }
        size = len(left) + len(right) if eligibility else max(len(left), len(right))
        weights = [
            [
                sum(
                    c.get("matching_weight", 1)
                    for c in comparisons[(left[i], right[j])]
                    if c["passed"] is True
                )
                if i < len(left) and j < len(right)
                else 0
                for j in range(size)
            ]
            for i in range(size)
        ]
        if eligibility:
            bonus = 1 + sum(abs(w) for row in weights for w in row)
            for i in range(len(left)):
                for j in range(len(right)):
                    weights[i][j] = (
                        (bonus + weights[i][j])
                        if eligibility(reference[left[i]], actual[right[j]])
                        else -bonus * size
                    )
        optimum, selected = assignment(weights)
        for i, j in selected:
            if (
                i < len(left)
                and j < len(right)
                and (
                    eligibility is None
                    or eligibility(reference[left[i]], actual[right[j]])
                )
            ):
                pairs[left[i]] = right[j]
        if size > 20:
            warnings.append("matching_group_exceeds_exact_ambiguity_limit:" + tool)
        for i, index in enumerate(left):
            possible = [
                right[j] if j < len(right) else None
                for j in range(size)
                if (
                    j >= len(right)
                    or eligibility is None
                    or eligibility(reference[index], actual[right[j]])
                )
                and (size > 20 or forced_score(weights, i, j) == optimum)
            ]
            alternatives[index] = list(dict.fromkeys(possible))
            if index not in pairs:
                continue
            chosen = pairs[index]
            checks = [dict(c) for c in comparisons[index, chosen]]
            for check_index, check in enumerate(checks):
                values = {
                    comparisons[index, candidate][check_index]["passed"]
                    if candidate is not None
                    else None
                    for candidate in alternatives[index]
                }
                if len(values) != 1:
                    check.update(passed=None, reason="ambiguous_optimal_matching")
            details[index] = checks
    return {
        "pairs": pairs,
        "alternatives": alternatives,
        "checks": details,
        "warnings": warnings,
        "objective": "same_tool_maximum_cardinality_then_known_parameter_pass_count",
        "uses_order_or_effects": False,
    }


def match_operations(
    reference: list,
    actual: list,
    user_name: str = "",
    comparator=None,
    eligibility=None,
) -> dict[str, Any]:
    """Refine parameter pairing using explicit reference provenance, never outcomes as goals."""
    from .checks import unresolved

    result = _match_operations(reference, actual, user_name, comparator, eligibility)
    if not any(unresolved(r["args"]) for r in reference):
        return result
    for _ in range(len(reference) + 1):
        bindings = ReferenceBindings(reference, actual, result)

        def compare(ref, call, name):
            checks = (
                comparator(ref, call, name)
                if comparator
                else compare_args(ref["tool"], call["args"], ref["args"], name)
            )
            return bindings.checks(ref, call, checks, name)

        updated = _match_operations(reference, actual, user_name, compare, eligibility)
        if updated == result:
            return updated
        result = updated
    # Cyclic or unstable provenance must never become guessed credit.
    for checks in result["checks"].values():
        for check in checks:
            if "reference_expected" in check:
                check.update(passed=None, reason="reference_binding_did_not_converge")
    result["warnings"].append("reference_binding_did_not_converge")
    return result

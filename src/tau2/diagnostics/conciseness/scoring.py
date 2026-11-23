"""Deterministic coverage, bounded TREC length efficiency and honest unknowns."""

import unicodedata
from collections import Counter, defaultdict
from statistics import mean


def response_length(replies):
    """Count NFC non-whitespace characters; utterance splitting adds no budget."""
    return sum(
        not char.isspace()
        for row in replies
        for char in unicodedata.normalize("NFC", row["text"])
    )


def harmonic_score(recall, efficiency):
    """Return F3 on a 0–100 scale, emphasizing complete feedback."""
    return (
        100 * 10 * recall * efficiency / (9 * efficiency + recall)
        if recall or efficiency
        else 0.0
    )


def _point(matched, optional, required, length, budget):
    recall = matched / required
    allowance = budget.base + budget.per_nugget * (
        matched + min(optional, budget.optional_cap * matched)
    )
    efficiency = min(1.0, allowance / length) if matched and length else 0.0
    return {
        "required_coverage": recall,
        "length_budget": allowance,
        "length_efficiency": efficiency,
        "feedback_score": harmonic_score(recall, efficiency),
    }


def aligned_groups(inventory, alignment):
    """Use the same deduplicated, unanimous matches for scoring and history."""
    decisions = {m["nugget_id"]: m["status"] for m in alignment["matches"]}
    groups = defaultdict(list)
    for nugget in inventory["nuggets"]:
        groups[nugget["equivalence_group"]].append(nugget)
    for group in groups.values():
        kind = (
            "required"
            if any(n["necessity"] == "required" for n in group)
            else "optional"
        )
        statuses = {decisions[n["id"]] for n in group}
        yield group, kind, next(iter(statuses)) if len(statuses) == 1 else "uncertain"


def score_window(inventory, alignment, replies, budget=None):
    """Count unique obligations; unknown matches retain their denominator."""
    counts = {kind: Counter() for kind in ("required", "optional")}
    for _, kind, status in aligned_groups(inventory, alignment):
        counts[kind][status] += 1
    required = sum(counts["required"].values())
    if not required:
        raise ValueError("Cannot score an empty required inventory")
    matched = counts["required"]["matched"]
    unknown = counts["required"]["uncertain"]
    optional = counts["optional"]["matched"]
    optional_unknown = counts["optional"]["uncertain"]
    length = response_length(replies)
    lower_r, upper_r = matched / required, (matched + unknown) / required
    result = {
        "status": "evidence_incomplete" if unknown or optional_unknown else "measured",
        "required_total": required,
        "required_matched": matched,
        "required_uncertain": unknown,
        "optional_matched": optional,
        "optional_uncertain": optional_unknown,
        "response_length": length,
        "required_coverage": lower_r if not unknown else None,
        "required_coverage_lower": lower_r,
        "required_coverage_upper": upper_r,
        "length_budget": None,
        "length_efficiency": None,
        "feedback_score": None,
        "calibrated": budget is not None,
        "reason": None if budget else "uncalibrated_language",
        "findings": alignment["findings"],
    }
    if budget is None:
        return result
    lower = _point(matched, optional, required, length, budget)
    upper = _point(
        matched + unknown, optional + optional_unknown, required, length, budget
    )
    for name in lower:
        result[name] = lower[name] if abs(lower[name] - upper[name]) < 1e-9 else None
        result[name + "_lower"] = lower[name]
        result[name + "_upper"] = upper[name]
    return result


def trial_summary(windows):
    """Equal window weights; never transfer unused length allowances."""
    applicable = [w for w in windows if w.get("status") != "not_applicable"]
    scored = [w for w in applicable if "required_total" in w]
    result = {
        "delivered_windows": len(windows),
        "applicable_windows": len(applicable),
        "scored_windows": len(scored),
        "window_coverage": len(scored) / len(applicable) if applicable else None,
        "response_length": sum(w.get("response_length", 0) for w in windows),
        "required_total": sum(w["required_total"] for w in scored),
        "required_matched": sum(w["required_matched"] for w in scored),
        "required_uncertain": sum(w["required_uncertain"] for w in scored),
    }
    for metric in (
        "required_coverage",
        "length_efficiency",
        "feedback_score",
    ):
        values = [w.get(metric) for w in applicable]
        result[metric] = (
            mean(values) if values and all(v is not None for v in values) else None
        )
    return result


def aggregate_reports(rows):
    """Report task-equal averages and missing cohorts without selecting winners."""
    records = []
    for row in rows:
        measure = row.get("summary", {}).get("measures", {}).get("conciseness")
        if measure and not measure.get("diagnostic_only"):
            records.append((row, measure))
    versions = {m.get("version", "tau-feedback-conciseness/1") for _, m in records}
    if len(versions) > 1:
        buckets = defaultdict(list)
        for row in rows:
            measure = row.get("summary", {}).get("measures", {}).get("conciseness")
            version = (
                measure.get("version", "tau-feedback-conciseness/1")
                if measure
                else "unscored"
            )
            buckets[version].append(row)
        return {
            "total_trials": len(rows),
            "groups": {
                version: aggregate_reports(bucket)
                for version, bucket in sorted(buckets.items())
            },
        }
    result = {
        "version": next(iter(versions), None),
        "total_trials": len(rows),
        "evaluated_trials": len(records),
        "statuses": dict(
            Counter(
                (r.get("summary", {}).get("measures", {}).get("conciseness") or {}).get(
                    "status", "unavailable"
                )
                for r in rows
            )
        ),
        "scored_trials": sum(
            m.get("trial", {}).get("feedback_score") is not None for _, m in records
        ),
    }
    result["score_coverage"] = result["scored_trials"] / len(rows) if rows else None
    for metric in (
        "required_coverage",
        "length_efficiency",
        "feedback_score",
    ):
        tasks = defaultdict(list)
        for row, measure in records:
            value = measure.get("trial", {}).get(metric)
            if value is not None:
                # Domain/language separate benchmark cells, not run paths: trials
                # for the same task in separate saved shards still share weight.
                tasks[
                    (measure.get("domain"), measure.get("language"), row.get("task_id"))
                ].append(value)
        result[metric] = mean(mean(v) for v in tasks.values()) if tasks else None
        result[metric + "_tasks"] = len(tasks)
    windows = [w for _, m in records for w in m.get("windows", [])]
    total = sum(w.get("required_total", 0) for w in windows)
    matched = sum(w.get("required_matched", 0) for w in windows)
    unknown = sum(w.get("required_uncertain", 0) for w in windows)
    result["micro_required_coverage"] = (
        matched / total if total and not unknown else None
    )
    result["required_total"] = total
    result["required_matched"] = matched
    result["required_uncertain"] = unknown
    result["outcomes"] = {
        state: {
            "windows": len(bucket),
            "feedback_score": mean(values)
            if (
                values := [
                    w["feedback_score"]
                    for w in bucket
                    if w.get("feedback_score") is not None
                ]
            )
            else None,
        }
        for state in sorted({w.get("outcome", "unknown") for w in windows})
        if (bucket := [w for w in windows if w.get("outcome", "unknown") == state])
    }
    valid_rewards = [
        r["reward"]
        for r in rows
        if r.get("reward") is not None
        and r.get("termination_reason")
        not in {"infrastructure_error", "user_error", "unexpected_error"}
    ]
    result["task_success_rate"] = (
        mean(abs(r - 1) <= 1e-6 for r in valid_rewards) if valid_rewards else None
    )
    return result

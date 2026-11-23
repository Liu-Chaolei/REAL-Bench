"""Stage 4: deterministic directional counts; no model, credentials or network."""

from collections import Counter
from typing import Any

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.evaluation import DIRECTIONS, METHOD, materialize
from tau2.diagnostics.intent.io import digest, index_records
from tau2.metrics.aggregation import task_key, task_mean


def harmonic(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def direction_counts(value: dict[str, Any]) -> dict[str, Any]:
    counts = Counter(row["status"] for row in value.get("rows", []))
    return {
        "total": sum(counts.values()) - counts["ignored"],
        **{
            state: counts[state]
            for state in (
                "matched",
                "changed",
                "missing",
                "extra",
                "ignored",
                "uncertain",
            )
        },
    }


def ratio(counts: dict[str, int]) -> float:
    return counts["matched"] / counts["total"] if counts["total"] else 1.0


def score_direction(value: dict[str, Any], direction: str) -> dict[str, Any]:
    if value.get("status") not in {"complete", "uncertain", "error", "unavailable"}:
        raise ValueError("Invalid direction status")
    if value.get("status") in {"complete", "uncertain"} or (
        "repetition_status" in value and "judgments" in value
    ):
        items = value["items"]
        if digest(items) != value["items_sha256"]:
            raise ValueError("Frozen item checksum differs")
        regenerated = materialize(value["judgments"], items, direction)
        if regenerated != value["rows"]:
            raise ValueError("Rows differ from validated frozen item judgments")
        uncertain = any(row["status"] == "uncertain" for row in regenerated)
        expected = next(
            state
            for state in ("error", "unavailable", "uncertain", "complete")
            if state
            in {
                "uncertain" if uncertain else "complete",
                value.get("repetition_status", "complete"),
            }
        )
        if expected != value["status"]:
            raise ValueError("Direction status disagrees with item judgments")
    counts = direction_counts(value)
    components = {}
    for component in sorted({row["component"] for row in value.get("rows", [])}):
        rows = [row for row in value["rows"] if row["component"] == component]
        subtotal = direction_counts({"rows": rows})
        components[component] = subtotal | {
            "score": None
            if subtotal["uncertain"] or value["status"] != "complete"
            else ratio(subtotal)
        }
    return {
        "status": value["status"],
        "counts": counts,
        "score": ratio(counts) if value["status"] == "complete" else None,
        "components": components,
    } | ({"reason": value["reason"]} if value.get("reason") else {})


def score_record(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Score frozen facts, validating duplicate adjustments by deterministic replay."""
    if evaluation.get("version") == "tau-real-intent/2" or "repetition" in evaluation:
        from tau2.diagnostics.intent.repetition import validate_final

        if evaluation.get("version") != "tau-real-intent/2":
            raise ValueError("Unknown or missing repetition-aware intent version")
        validate_final(evaluation)
        # Independently verify the immutable base, including its frozen fact rows.
        content_scores = score_record(evaluation["content_evaluation"])
    else:
        content_scores = None
    if evaluation.get("method") != METHOD:
        raise ValueError(
            "Evaluation records require method=bidirectional_text; run intent evaluate with original texts"
        )
    directions = {name: score_direction(evaluation[name], name) for name in DIRECTIONS}
    statuses = {value["status"] for value in directions.values()}
    expected = next(
        state
        for state in ("error", "unavailable", "uncertain", "complete")
        if state in statuses
    )
    if evaluation["status"] != expected:
        raise ValueError("Evaluation status disagrees with direction statuses")
    precision, recall = directions["backward"]["score"], directions["forward"]["score"]
    valid = precision is not None and recall is not None
    result = {
        "score_version": "task-equal-intent-v1",
        "method": METHOD,
        "id": evaluation["id"],
        "evaluation_sha256": digest(evaluation),
        "profile_sha256": evaluation.get("profile_sha256"),
        "status": evaluation["status"],
        "precision": precision,
        "recall": recall,
        "f1": harmonic(precision, recall) if valid else None,
        **directions,
    } | ({"reason": evaluation["reason"]} if evaluation.get("reason") else {})
    if content_scores is not None:
        record = evaluation["repetition"]
        judgment = record.get("judgment", {})
        result.update(
            version=evaluation["version"],
            content_scores=content_scores,
            repetition={
                "version": record["version"],
                "status": record["status"],
                "total_goal_count": len(record["input"]["goal_paths"]),
                "checked_goal_count": len(judgment.get("checked_goal_paths", [])),
                "extra_goal_count": sum(
                    len(g["extra_goal_paths"]) for g in judgment.get("groups", [])
                ),
                "uncertain_goal_count": len(judgment.get("uncertain_goal_paths", [])),
                "groups": judgment.get("groups", []),
                "adjustments": evaluation["repetition_adjustments"],
                "reason": record.get("reason"),
            },
        )
    return result


def score_batch(
    evaluations: list[dict[str, Any]], group_by: list[str] | None = None
) -> dict[str, Any]:
    index_records(evaluations)
    profiles = {
        row["profile_sha256"] for row in evaluations if row.get("profile_sha256")
    }
    if len(profiles) > 1:
        raise ValueError("Score different profile versions in separate batches")
    if len({row.get("version", "tau-real-intent/1") for row in evaluations}) > 1:
        raise ValueError("Score different intent rule versions in separate batches")
    records = [score_record(row) for row in evaluations]
    valid = [row for row in records if row["f1"] is not None]
    grouped = {}
    for field in group_by or []:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for evaluation in evaluations:
            label = str(evaluation.get("metadata", {}).get(field, "__missing__"))
            buckets.setdefault(label, []).append(evaluation)
        grouped[field] = {
            label: {
                key: value
                for key, value in score_batch(bucket).items()
                if key not in {"records", "groups"}
            }
            for label, bucket in sorted(buckets.items())
        }
    return {
        "method": METHOD,
        "pipeline_version": PIPELINE_VERSION,
        "profile_sha256s": sorted(profiles),
        "records": records,
        "groups": grouped,
        "precision": task_mean(
            (task_key(e), r["precision"])
            for e, r in zip(evaluations, records)
            if r["f1"] is not None
        ),
        "recall": task_mean(
            (task_key(e), r["recall"])
            for e, r in zip(evaluations, records)
            if r["f1"] is not None
        ),
        "macro_f1": task_mean(
            (task_key(e), r["f1"]) for e, r in zip(evaluations, records)
        ),
        "evaluation_log": {
            "coverage": {
                "total": len(records),
                "scored": len(valid),
                "unavailable": len(records) - len(valid),
                "rate": len(valid) / len(records) if records else None,
                "statuses": dict(Counter(row["status"] for row in records)),
            }
        },
    }

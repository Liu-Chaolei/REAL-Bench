"""Stage 4: deterministic directional counts; no model, credentials or network."""

from collections import Counter
from typing import Any

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.evaluation import DIRECTIONS, METHOD, materialize
from tau2.diagnostics.intent.io import digest, index_records


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
    if value.get("status") in {"complete", "uncertain"}:
        items = value["items"]
        if digest(items) != value["items_sha256"]:
            raise ValueError("Frozen item checksum differs")
        regenerated = materialize(value["judgments"], items, direction)
        if regenerated != value["rows"]:
            raise ValueError("Rows differ from validated frozen item judgments")
        uncertain = any(row["status"] == "uncertain" for row in regenerated)
        if uncertain != (value["status"] == "uncertain"):
            raise ValueError("Direction status disagrees with item judgments")
    counts = direction_counts(value)
    components = {}
    for component in sorted({row["component"] for row in value.get("rows", [])}):
        rows = [row for row in value["rows"] if row["component"] == component]
        subtotal = direction_counts({"rows": rows})
        components[component] = subtotal | {
            "score": None if subtotal["uncertain"] else ratio(subtotal)
        }
    return {
        "status": value["status"],
        "counts": counts,
        "score": ratio(counts) if value["status"] == "complete" else None,
        "components": components,
    } | ({"reason": value["reason"]} if value.get("reason") else {})


def score_record(evaluation: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "method": METHOD,
        "id": evaluation["id"],
        "evaluation_sha256": digest(evaluation),
        "profile_sha256": evaluation.get("profile_sha256"),
        "status": evaluation["status"],
        "precision": precision,
        "recall": recall,
        "f1": harmonic(precision, recall) if valid else None,
        "complete_match": precision == recall == 1 if valid else None,
        **directions,
    } | ({"reason": evaluation["reason"]} if evaluation.get("reason") else {})


def score_batch(
    evaluations: list[dict[str, Any]], group_by: list[str] | None = None
) -> dict[str, Any]:
    index_records(evaluations)
    profiles = {
        row["profile_sha256"] for row in evaluations if row.get("profile_sha256")
    }
    if len(profiles) > 1:
        raise ValueError("Score different profile versions in separate batches")
    records = [score_record(row) for row in evaluations]
    valid = [row for row in records if row["f1"] is not None]
    micro = None
    if valid:
        totals = {
            name: {
                key: sum(row[name]["counts"][key] for row in valid)
                for key in valid[0][name]["counts"]
            }
            for name in DIRECTIONS
        }
        precision, recall = ratio(totals["backward"]), ratio(totals["forward"])
        micro = {
            "precision": precision,
            "recall": recall,
            "f1": harmonic(precision, recall),
            **totals,
        }
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
        "micro": micro,
        "macro_f1": sum(row["f1"] for row in valid) / len(valid) if valid else None,
        "complete_match_rate": sum(row["complete_match"] for row in valid) / len(valid)
        if valid
        else None,
        "coverage": {
            "total": len(records),
            "scored": len(valid),
            "unavailable": len(records) - len(valid),
            "rate": len(valid) / len(records) if records else None,
            "statuses": dict(Counter(row["status"] for row in records)),
        },
    }

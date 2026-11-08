"""Key-free review export and deterministic fitting of human-approved budgets."""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from pydantic import Field

from tau2.config import DEFAULT_FEEDBACK_OPTIONAL_CAP
from tau2.diagnostics.conciseness.evidence import build_windows, generation_input
from tau2.diagnostics.conciseness.models import Calibration, FeedbackConfig, StrictModel
from tau2.diagnostics.conciseness.scoring import response_length
from tau2.diagnostics.intent.io import digest, write_json


class HumanReview(StrictModel):
    """An explicit human attestation, never synthesized by the grader."""

    reviewer: str = Field(min_length=1)
    approved: bool
    inventory_correct: bool
    natural_and_complete: bool
    required_count: int = Field(ge=1)
    optional_count: int = Field(ge=0)
    concise_response: str = Field(min_length=1)


def export_review(results_path, output):
    """Export deterministic, task-blind-to-answer review cases without model calls."""
    from tau2.data_model.simulation import Results
    from tau2.diagnostics.suite import discover_runs, load_evidence

    cases, excluded = [], []
    for run in discover_runs(results_path):
        meta = Results.load_metadata(run)
        base = run if run.is_dir() else run.parent
        for sim in Results.iter_simulations(run):
            root = (
                base
                / "artifacts"
                / f"task_{sim.task_id}"
                / f"sim_{sim.id}"
                / "diagnostics"
            )
            try:
                evidence = load_evidence(root / "evidence")
                for window in build_windows(evidence, FeedbackConfig()):
                    identity = digest(
                        {
                            "domain": evidence.domain,
                            "task": sim.task_id,
                            "simulation": sim.id,
                            "window": window["id"],
                        }
                    )
                    if window["status"] != "ready":
                        excluded.append({"id": identity, "reason": window["reason"]})
                        continue
                    cases.append(
                        {
                            "id": identity,
                            "task_id": sim.task_id,
                            "simulation_id": sim.id,
                            "window_id": window["id"],
                            "language": meta.info.language,
                            "evidence_sha256": digest(evidence.model_dump(mode="json")),
                            "split": "validation"
                            if int(
                                digest(
                                    {"task": sim.task_id, "domain": evidence.domain}
                                )[:8],
                                16,
                            )
                            % 5
                            == 0
                            else "development",
                            "blind_input": generation_input(window, []),
                            "candidate_replies": window["replies"],
                            "reviews": [],
                        }
                    )
            except Exception as exc:
                excluded.append({"simulation_id": sim.id, "reason": type(exc).__name__})
    value = {
        "version": "tau-feedback-review/1",
        "instructions": "Two humans independently inspect blind_input before candidate_replies. Add HumanReview records: reviewer, approved, inventory_correct, natural_and_complete, required_count, optional_count, concise_response. Do not use model-generated attestations. Keep tasks disjoint between development and validation.",
        "cases": cases,
        "excluded": excluded,
    }
    write_json(Path(output), value)
    return {"exported_windows": len(cases), "excluded_windows": len(excluded)}


def fit_calibration(review_path, calibration_id, provenance):
    """Fit nonnegative budgets on development only; retain holdout diagnostics."""
    review = json.loads(Path(review_path).read_text())
    if review.get("version") != "tau-feedback-review/1":
        raise ValueError("Unsupported review format")
    groups, tasks, ids = defaultdict(lambda: defaultdict(list)), {}, set()
    optional_cap = DEFAULT_FEEDBACK_OPTIONAL_CAP
    for case in review["cases"]:
        if case["id"] in ids:
            raise ValueError("Duplicate review case")
        ids.add(case["id"])
        if case["split"] not in {"development", "validation"}:
            raise ValueError("Unknown review split")
        task = (case["language"], case["task_id"])
        if task in tasks and tasks[task] != case["split"]:
            raise ValueError("Development and validation must have disjoint tasks")
        tasks[task] = case["split"]
        reviews = [HumanReview.model_validate(r) for r in case["reviews"]]
        if len({r.reviewer for r in reviews}) < 2 or not all(
            r.approved and r.inventory_correct and r.natural_and_complete
            for r in reviews
        ):
            raise ValueError("Each case needs two approving human reviews")
        if len({(r.required_count, r.optional_count) for r in reviews}) != 1:
            raise ValueError("Resolve human inventory count disagreements first")
        for row in reviews:
            length = response_length([{"text": row.concise_response}])
            if not length:
                raise ValueError("Empty concise reference")
            groups[case["language"]][case["split"]].append(
                (
                    row.required_count
                    + min(row.optional_count, optional_cap * row.required_count),
                    length,
                )
            )
    if not groups:
        raise ValueError("No reviewed cases")
    budgets, validation = {}, {}
    for language, splits in groups.items():
        dev, held = splits["development"], splits["validation"]
        if len(dev) < 2 or not held:
            raise ValueError("Every language needs development and held-out references")
        x = np.array([r[0] for r in dev], dtype=float)
        y = np.array([r[1] for r in dev], dtype=float)
        # Compare feasible boundary and interior least-squares candidates.
        slope = float(x @ y / (x @ x))
        candidates = [(0.0, slope)]
        if len(set(x)) > 1:
            intercept, coefficient = np.linalg.lstsq(
                np.column_stack([np.ones(len(x)), x]), y, rcond=None
            )[0]
            if intercept >= 0 and coefficient > 0:
                candidates.append((float(intercept), float(coefficient)))
        intercept, coefficient = min(
            candidates, key=lambda p: float(np.square(y - p[0] - p[1] * x).sum())
        )
        scale = max(
            1.0,
            float(
                np.quantile(y / (intercept + coefficient * x), 0.95, method="higher")
            ),
        )
        budgets[language] = {
            "base": intercept * scale,
            "per_nugget": coefficient * scale,
            "optional_cap": optional_cap,
        }
        validation[language] = {
            "development_references": len(dev),
            "heldout_references": len(held),
            "heldout_within_budget": sum(
                length <= scale * (intercept + coefficient * n) for n, length in held
            )
            / len(held),
        }
    calibration = Calibration.model_validate(
        {
            "id": calibration_id,
            "reviewed": True,
            "provenance": provenance,
            "review_sha256": digest(review),
            "languages": budgets,
        }
    )
    return {
        "calibration": calibration.model_dump(mode="json"),
        "validation": validation,
        "fit": "nonnegative_least_squares_then_development_95th_percentile_envelope",
    }

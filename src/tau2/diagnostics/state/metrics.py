"""Progressivity from causal deltas; Grounding from state-use opportunities."""

from collections import Counter
from pathlib import Path

from tau2.diagnostics.intent.io import write_json
from tau2.diagnostics.state.events import build_events
from tau2.diagnostics.state.projector import PROJECTOR_VERSION, project_state
from tau2.diagnostics.state.semantic import SemanticInterpreter
from tau2.metrics.aggregation import task_mean

VERSION = "tau-state-interaction/2"


def evaluate_state_interaction(
    task, evidence, output_directory, model=None, *, interpreter=None
):
    """Interpret sequential prefixes and persist the same trace used for scoring."""
    if interpreter is None and model is not None:
        interpreter = SemanticInterpreter(
            model, evidence.source.get("state_capabilities", {})
        )
    windows, versions, state = project_state(task, evidence, interpreter)
    progress = Counter(w.progressivity for w in windows if w.scoreable)
    opportunities = [o for w in windows for o in w.opportunities]
    ground = Counter(o["status"] for o in opportunities)
    denominator = sum(progress[k] for k in ("forward", "stagnated", "regressed"))
    ground_denominator = sum(
        ground[k] for k in ("consistent", "inconsistent", "unsupported")
    )
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = dict(
        version=VERSION,
        projector_version=PROJECTOR_VERSION,
        status=(
            "partial"
            if any(not w.scoreable and w.issues for w in windows)
            else "complete"
        )
        if interpreter is not None
        else "unavailable",
        progressivity=dict(
            progress,
            eligible_windows=denominator,
            forward_rate=progress["forward"] / denominator if denominator else None,
        ),
        grounding=dict(
            ground,
            eligible_opportunities=ground_denominator,
            consistent_rate=ground["consistent"] / ground_denominator
            if ground_denominator
            else None,
        ),
        evaluation_log=dict(
            windows=len(windows),
            unscored_windows=sum(not w.scoreable for w in windows),
            uncertain_opportunities=ground["uncertain"],
            not_applicable=ground["not_applicable"],
            incomplete_events=[i for w in windows for i in w.issues],
        ),
        trace_path="state_trace.json",
    )
    if interpreter is None:
        result["reason"] = "state_interaction_model_not_configured"
    write_json(
        directory / "state_trace.json",
        dict(
            version=PROJECTOR_VERSION,
            events=[e.model_dump(mode="json") for e in build_events(task, evidence)],
            versions=versions,
            transitions=[w.model_dump(mode="json") for w in windows],
            final_state=state.model_dump(mode="json"),
        ),
    )
    write_json(directory / "state_interaction.json", result)
    return result


def summarize_state_interaction(results):
    """Keep versions apart; use the normal per-task summary helper."""
    valid = [
        (i, r)
        for i, r in enumerate(results)
        if r
        and r.get("version") == VERSION
        and r.get("status") == "complete"
        and not r.get("diagnostic_only")
    ]

    def average(group, field):
        return task_mean(
            (tuple(r.get("task_key") or ("trial", i)), r[group].get(field))
            for i, r in valid
        )

    return dict(
        version=VERSION,
        progressivity={"forward_rate": average("progressivity", "forward_rate")},
        grounding={"consistent_rate": average("grounding", "consistent_rate")},
        evaluation_log={"scored": len(valid), "total_trials": len(results)},
    )

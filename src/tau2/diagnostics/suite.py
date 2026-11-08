"""Independent saved-evidence evaluation, usable live and from the CLI."""

import asyncio
import hashlib
import json
from pathlib import Path

from tau2.data_model.diagnostics import DiagnosticEvidence
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.io import digest, write_json
from tau2.diagnostics.model import RequestBudget, TauJsonModel


def implementation_digest():
    """Cache identity includes every score definition and shipped resource."""
    root = Path(__file__).parent
    files = (
        list(root.rglob("*"))
        + [root.parent / "metrics/real_voice.py"]
        + list((root.parent / "utils/transcript").glob("*.py"))
    )
    h = hashlib.sha256()
    for p in sorted(
        p
        for p in files
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def load_evidence(directory):
    """Verify every collected artifact before trusting content or state."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Evidence path escapes its artifact directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Evidence hash mismatch: " + relative)
    return DiagnosticEvidence.model_validate_json(
        (directory / "evidence.json").read_text()
    )


def evaluate_evidence(task, evidence, directory, config, only=None):
    """Score independent measures; failures never replace a task reward."""
    directory = Path(directory)
    if evidence.source.get("task_sha256") and evidence.source["task_sha256"] != digest(
        task.model_dump(mode="json")
    ):
        raise ValueError("Task differs from collected diagnostic evidence")
    profile = digest(
        {"config": config.model_dump(), "implementation": implementation_digest()}
    )
    output = directory.parent / "evaluations" / profile
    budget = RequestBudget(config.max_requests)
    results = {}
    for measure in only if only is not None else config.measures:
        path = output / (measure + ".json")
        key = digest(
            {
                "evidence": evidence.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
                "profile": profile,
                "measure": measure,
            }
        )
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("input_key") == key and previous.get("status") in {
            "complete",
            "measured",
            "not_applicable",
        }:
            results[measure] = previous
            continue
        try:
            if measure == "intent":
                from tau2.diagnostics.intent.call import evaluate_call

                result = asyncio.run(
                    evaluate_call(evidence, config, output / "intent", budget)
                )
            elif measure == "execution":
                if evidence.domain != "gaia2":
                    result = {
                        "status": "not_applicable",
                        "reason": "domain_adapter_not_implemented",
                    }
                else:
                    from tau2.diagnostics.execution.gaia2 import (
                        Gaia2ExecutionAdapter,
                        metadata,
                    )
                    from tau2.domains.gaia2.utils import GAIA2_DATA_DIR

                    result = Gaia2ExecutionAdapter(metadata(GAIA2_DATA_DIR)).evaluate(
                        task,
                        evidence,
                        TauJsonModel(config, "execution", output / "requests", budget),
                    )
            elif measure == "voice":
                from tau2.diagnostics.voice import evaluate_voice

                result = evaluate_voice(evidence, directory)
            elif measure == "conciseness":
                from tau2.diagnostics.conciseness.evaluator import evaluate_feedback

                result = evaluate_feedback(
                    evidence, config, directory, output / "conciseness", budget
                )
            elif measure == "interaction":
                from tau2.diagnostics.interaction import evaluate_interaction

                result = evaluate_interaction(evidence, config, output, budget)
            else:
                raise ValueError("Unknown diagnostic measure: " + measure)
        except Exception as exc:
            result = {
                "status": "grader_error",
                "reason": type(exc).__name__,
            }
        result["diagnostic_only"] = not evidence.complete
        result["input_key"] = key
        write_json(path, result)
        results[measure] = result
    # Keep earlier successful measures when a CLI call selects a subset.
    summary_path = output / "summary.json"
    stored = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    prior = {
        name: value
        for name, value in stored.get("measures", {}).items()
        if value.get("input_key")
        == digest(
            {
                "evidence": evidence.model_dump(mode="json"),
                "task": task.model_dump(mode="json"),
                "profile": profile,
                "measure": name,
            }
        )
    }
    combined = {**prior, **results}
    stages = evidence.stages.get("stages", [])
    summary = {
        "version": "tau-real-diagnostics/1",
        "profile": profile,
        "simulation_id": evidence.simulation_id,
        "task_id": evidence.task_id,
        "measures": combined,
        "requests_this_run": budget.used,
        "coverage": {
            "total_stages": len(stages),
            "delivered_stages": sum(bool(r.get("started")) for r in stages),
            "fully_delivered_stages": sum(bool(r.get("completed")) for r in stages),
        },
        "source": evidence.source,
    }
    write_json(summary_path, summary)
    write_json(
        directory.parent / "latest.json",
        {"directory": str(output), "profile": profile, "summary": summary},
    )
    return {"directory": str(output), "profile": profile, "summary": summary}


def discover_runs(path):
    """Resolve a single saved run or a benchmark matrix without losing cells."""
    root = Path(path)
    if root.is_file() or (root / "results.json").exists():
        return [root]
    runs = sorted({p.parent for p in root.rglob("results.json")})
    if not runs:
        raise ValueError("No saved tau results found")
    return runs


def run_saved(results_path, config, only=None, dry_run=False):
    """Evaluate saved calls with bounded concurrency and independent budgets."""
    from concurrent.futures import ThreadPoolExecutor

    from tau2.data_model.simulation import Results

    root = Path(results_path)
    jobs = []
    missing = []
    for run in discover_runs(root):
        meta = Results.load_metadata(run)
        tasks = {task.id: task for task in meta.tasks}
        observed = set()
        for sim in Results.iter_simulations(run):
            observed.add((sim.task_id, sim.trial or 0))
            base = run if run.is_dir() else run.parent
            directory = (
                base
                / "artifacts"
                / f"task_{sim.task_id}"
                / f"sim_{sim.id}"
                / "diagnostics"
                / "evidence"
            )
            jobs.append(
                (
                    run,
                    sim,
                    tasks[sim.task_id],
                    directory,
                    meta.info.environment_info.domain_name,
                    meta.info.language,
                )
            )
        for task in meta.tasks:
            for trial in range(meta.info.num_trials):
                if (task.id, trial) not in observed:
                    missing.append(
                        {
                            "run": str(run),
                            "task_id": task.id,
                            "trial": trial,
                            "status": "unavailable",
                            "reason": "missing_trial_trajectory",
                        }
                    )

    def process(job):
        run, sim, task, directory, domain, language = job
        row = {
            "simulation_id": sim.id,
            "task_id": sim.task_id,
            "trial": sim.trial,
            "run": str(run),
            "evidence": str(directory),
            "termination_reason": sim.termination_reason.value,
            "reward": sim.reward_info.reward if sim.reward_info else None,
        }
        selected = only if only is not None else config.measures
        if not directory.exists() and selected != ["interaction"]:
            return row | {
                "status": "unavailable",
                "reason": "historical_run_missing_diagnostic_evidence",
            }
        try:
            if directory.exists():
                evidence = load_evidence(directory)
            else:
                from tau2.diagnostics.interaction import conversation_evidence

                evidence = conversation_evidence(sim, task, domain)
            if evidence.simulation_id != sim.id or evidence.task_id != sim.task_id:
                raise ValueError("Evidence identity differs from trajectory")
            if dry_run:
                planned = row | {"status": "planned", "measures": selected}
                if "conciseness" in selected:
                    from tau2.diagnostics.conciseness.evidence import preflight

                    planned["conciseness"] = preflight(evidence, config.conciseness)
                if "interaction" in selected:
                    from tau2.diagnostics.interaction import build_input

                    payload = build_input(evidence)
                    planned.update(
                        target_utterances=len(payload["target_utterance_ids"]),
                        interaction_input_characters=len(
                            json.dumps(payload, ensure_ascii=False)
                        ),
                    )
                return planned
            effective = config
            if "conciseness" in selected and config.conciseness.language is None:
                effective = config.model_copy(
                    update={
                        "conciseness": config.conciseness.model_copy(
                            update={"language": language}
                        )
                    }
                )
            if "model" not in config.model_fields_set and evidence.source.get(
                "metrics_config"
            ):
                effective = effective.model_copy(
                    update={
                        "model": MetricsConfig.model_validate(
                            evidence.source["metrics_config"]
                        ).model
                    }
                )
            return (
                row
                | {"status": "evaluated"}
                | evaluate_evidence(task, evidence, directory, effective, only)
            )
        except Exception as exc:
            return row | {"status": "unavailable", "reason": type(exc).__name__}

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        rows = list(pool.map(process, jobs)) + missing
    report = summarize(rows)
    if not dry_run:
        write_json(
            (root if root.is_dir() else root.parent) / "diagnostics" / "report.json",
            report,
        )
    return report


def read_saved_reports(results_path):
    """Read latest score sidecars, including missing trajectories and measures."""
    from tau2.data_model.simulation import Results

    rows = []
    for run in discover_runs(results_path):
        meta = Results.load_metadata(run)
        seen = set()
        for sim in Results.iter_simulations(run):
            seen.add((sim.task_id, sim.trial or 0))
            base = run if run.is_dir() else run.parent
            latest = (
                base
                / "artifacts"
                / f"task_{sim.task_id}"
                / f"sim_{sim.id}"
                / "diagnostics"
                / "latest.json"
            )
            row = {
                "simulation_id": sim.id,
                "task_id": sim.task_id,
                "trial": sim.trial,
                "run": str(run),
                "reward": sim.reward_info.reward if sim.reward_info else None,
                "termination_reason": sim.termination_reason.value,
            }
            if latest.exists():
                row.update(json.loads(latest.read_text()))
            else:
                row.update(status="unavailable", reason="missing_diagnostic_scores")
            rows.append(row)
        for task in meta.tasks:
            for trial in range(meta.info.num_trials):
                if (task.id, trial) not in seen:
                    rows.append(
                        {
                            "task_id": task.id,
                            "trial": trial,
                            "run": str(run),
                            "status": "unavailable",
                            "reason": "missing_trial_trajectory",
                        }
                    )
    return summarize(rows)


def summarize(rows):
    """Coverage always includes missing trials; rates use explicit denominators."""
    from collections import Counter

    from tau2.diagnostics.voice import aggregate_voice

    profiles = {r.get("profile") for r in rows if r.get("profile")}
    if len(profiles) > 1:
        return {
            "version": "tau-real-diagnostics/1",
            "total_trials": len(rows),
            "records": rows,
            "groups": {
                profile: summarize([r for r in rows if r.get("profile") == profile])
                for profile in sorted(profiles)
            },
            "unscored_trials": sum(not r.get("profile") for r in rows),
            "reason": "different_profiles_reported_separately",
        }

    def intent_version(row):
        result = row.get("summary", {}).get("measures", {}).get("intent", {})
        return result.get("version", "tau-real-intent/1") if result else "unscored"

    versions = {intent_version(row) for row in rows} - {"unscored"}
    if len(versions) > 1:
        return {
            "version": "tau-real-diagnostics/1",
            "total_trials": len(rows),
            "records": rows,
            "groups": {
                version: summarize(
                    [row for row in rows if intent_version(row) == version]
                )
                for version in sorted(versions)
            },
            "unscored_trials": sum(intent_version(row) == "unscored" for row in rows),
            "reason": "different_intent_versions_reported_separately",
        }
    intent, voice, execution, statuses = [], [], [], {}
    for row in rows:
        summary = row.get("summary", {})
        for name in ("intent", "execution", "voice", "interaction", "conciseness"):
            result = summary.get("measures", {}).get(name)
            statuses.setdefault(name, Counter())[
                result.get("status", "unavailable") if result else "unavailable"
            ] += 1
        result = summary.get("measures", {}).get("intent", {})
        if result.get("method") == "bidirectional_text" and not result.get(
            "diagnostic_only"
        ):
            intent.append(result)
        result = summary.get("measures", {}).get("voice")
        if result and "metrics" in result and not result.get("diagnostic_only"):
            voice.append(result)
        result = summary.get("measures", {}).get("execution", {})
        if result:
            execution.append(result)
    # Intent score_record is already deterministic; pool its directional counts.
    valid = [r for r in intent if r.get("f1") is not None]
    repetitions = [result["repetition"] for result in intent if "repetition" in result]

    def fraction(direction):
        matched = sum(r[direction]["counts"]["matched"] for r in valid)
        total = sum(r[direction]["counts"]["total"] for r in valid)
        return matched / total if total else 1.0

    p, r = fraction("backward"), fraction("forward")
    from tau2.diagnostics.intent.scoring import harmonic

    execution_groups = {}
    from tau2.diagnostics.execution.scoring import aggregate, dimension

    for result in execution:
        if result.get("diagnostic_only") or not result.get("dimensions"):
            continue
        signature = digest(
            {
                k: {"status": v["status"], "coverage": v["coverage"]}
                for k, v in result["dimensions"].items()
            }
        )
        execution_groups.setdefault(signature, []).append(result)
    grouped = {}
    for signature, members in execution_groups.items():
        pooled = {}
        for name in ("tools", "parameters", "order", "effects"):
            parts = [m["dimensions"][name] for m in members]
            if name == "tools" and parts[0]["status"] != "not_applicable":
                expected = sum(p["reference_count"] for p in parts)
                observed = sum(p["actual_count"] for p in parts)
                matched = sum(p["matched"] for p in parts)
                value = (
                    2 * matched / (expected + observed) if expected + observed else None
                )
                pooled[name] = {
                    "score": value,
                    "lower": value,
                    "upper": value,
                    "coverage": 1.0,
                    "status": "measured",
                    "reference_count": expected,
                    "actual_count": observed,
                    "matched": matched,
                }
            else:
                pooled[name] = dimension([u for p in parts for u in p.get("units", [])])
        grouped[signature] = {
            "tasks": len(members),
            "dimensions": pooled,
            "total": aggregate(pooled),
            "profile": {
                k: {"status": v["status"], "coverage": v["coverage"]}
                for k, v in members[0]["dimensions"].items()
            },
        }
    total_stages = sum(
        row.get("summary", {}).get("coverage", {}).get("total_stages", 0)
        for row in rows
    )
    delivered_stages = sum(
        row.get("summary", {}).get("coverage", {}).get("delivered_stages", 0)
        for row in rows
    )
    report = {
        "version": "tau-real-diagnostics/1",
        "total_trials": len(rows),
        "records": rows,
        "statuses": {k: dict(v) for k, v in statuses.items()},
        "stage_coverage": {
            "total": total_stages,
            "delivered": delivered_stages,
            "rate": delivered_stages / total_stages if total_stages else None,
        },
        "termination_reasons": dict(
            Counter(row.get("termination_reason", "unknown") for row in rows)
        ),
        "intent": {
            "version": next(iter(versions)) if versions else None,
            "scored": len(valid),
            "micro": {"precision": p, "recall": r, "f1": harmonic(p, r)}
            if valid
            else None,
            "macro_f1": sum(x["f1"] for x in valid) / len(valid) if valid else None,
            "complete_match_rate": sum(x["complete_match"] for x in valid) / len(valid)
            if valid
            else None,
            "repetition": {
                "trials": len(repetitions),
                "completed": sum(r["status"] == "complete" for r in repetitions),
                "statuses": dict(Counter(r["status"] for r in repetitions)),
                "total_goal_count": sum(r["total_goal_count"] for r in repetitions),
                "checked_goal_count": sum(r["checked_goal_count"] for r in repetitions),
                "extra_goal_count": sum(r["extra_goal_count"] for r in repetitions),
                "uncertain_goal_count": sum(
                    r["uncertain_goal_count"] for r in repetitions
                ),
                "coverage": sum(r["status"] == "complete" for r in repetitions)
                / len(repetitions)
                if repetitions
                else None,
            },
        },
        "real_voice": aggregate_voice(voice + [None] * (len(rows) - len(voice))),
        "execution": {
            "scored": sum(
                x.get("total", {}).get("score") is not None
                and not x.get("diagnostic_only")
                for x in execution
            ),
            "totals": [x.get("total") for x in execution],
            "groups": grouped,
            "note": "Compare totals only within matching applicability and coverage profiles.",
        },
    }
    from tau2.diagnostics.interaction import summarize_interaction

    report["interaction"] = summarize_interaction(
        [row.get("summary", {}).get("measures", {}).get("interaction") for row in rows]
    )
    from tau2.diagnostics.conciseness.scoring import aggregate_reports

    report["conciseness"] = aggregate_reports(rows)
    return report

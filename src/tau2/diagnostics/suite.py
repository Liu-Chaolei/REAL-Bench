"""Independent saved-evidence evaluation, usable live and from the CLI."""

import asyncio
import hashlib
import json
from pathlib import Path

from tau2.data_model.diagnostics import DiagnosticEvidence
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.io import digest, write_json
from tau2.diagnostics.model import RequestBudget, TauJsonModel
from tau2.metrics.outcome import env_assertion_value


def implementation_digest():
    """Cache identity includes every score definition and shipped resource."""
    root = Path(__file__).parent
    files = (
        list(root.rglob("*"))
        + [
            root.parent / "metrics/real_voice.py",
            root.parent / "metrics/voice_interaction.py",
            root.parent / "voice/audio_activity.py",
            root.parent / "config.py",
        ]
        + list((root.parent / "metrics/active").rglob("*.py"))
        + list(
            (root.parents[2] / "data/tau2/metrics/persistent_task/templates").glob(
                "*.json"
            )
        )
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
        from tau2.domains.gaia2.interaction_variants import metric_enabled

        if not metric_enabled(task, measure):
            results[measure] = {
                "status": "not_applicable",
                "reason": "interaction_variant_scoring_scope",
            }
            continue
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
            if measure == "persistent_task":
                from tau2.persistent_tasks.scores import consolidate_score

                previous = consolidate_score(previous)
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
            elif measure == "state_interaction":
                from tau2.diagnostics.state.metrics import evaluate_state_interaction

                result = evaluate_state_interaction(
                    task,
                    evidence,
                    output,
                    model=TauJsonModel(
                        config, "state_interaction", output / "requests", budget
                    ),
                )
            elif measure == "persistent_task":
                result = (
                    evidence.source.get("active_metrics", {})
                    .get("scores", {})
                    .get("persistent_task")
                )
                if result is None:
                    result = {
                        "status": "not_applicable",
                        "reason": "active_metric_evidence_missing",
                    }
            else:
                raise ValueError("Unknown diagnostic measure: " + measure)
        except Exception as exc:
            result = {
                "status": "grader_error",
                "reason": type(exc).__name__,
            }
        if measure == "persistent_task":
            from tau2.persistent_tasks.scores import consolidate_score

            result = consolidate_score(result)
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
                    getattr(meta.info, "task_set_name", None),
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
        run, sim, task, directory, domain, language, task_set_name = job
        row = {
            "simulation_id": sim.id,
            "task_id": sim.task_id,
            "trial": sim.trial,
            "run": str(run),
            "domain": domain,
            "language": language,
            "task_set_name": task_set_name,
            "hidden_intent": (sim.info or {}).get("hidden_intent"),
            "persistent_tasks": (sim.info or {}).get("persistent_tasks"),
            "evidence": str(directory),
            "termination_reason": sim.termination_reason.value,
            **(
                {"env_assertion": env_assertion_value(sim)}
                if domain == "gaia2"
                else {"reward": sim.reward_info.reward if sim.reward_info else None}
            ),
        }
        selected = only if only is not None else config.measures
        if not directory.exists() and selected not in (
            ["interaction"],
            ["state_interaction"],
        ):
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
                if "state_interaction" in selected:
                    planned["state_interaction"] = {
                        "status": "planned",
                        "evidence_mode": evidence.source.get(
                            "interaction_evidence_mode", "recorded_events"
                        ),
                        "model": config.profile("state_interaction").model,
                        "semantic_requests": sum(
                            e.get("kind")
                            in {"user_turn", "agent_turn", "tool_requested"}
                            for e in evidence.state_events
                        ),
                        "shared_max_requests": config.max_requests,
                    }
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
                "domain": meta.info.environment_info.domain_name,
                "language": meta.info.language,
                "task_set_name": getattr(meta.info, "task_set_name", None),
                "hidden_intent": (sim.info or {}).get("hidden_intent"),
                "persistent_tasks": (sim.info or {}).get("persistent_tasks"),
                **(
                    {"env_assertion": env_assertion_value(sim)}
                    if meta.info.environment_info.domain_name == "gaia2"
                    else {"reward": sim.reward_info.reward if sim.reward_info else None}
                ),
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

    def variant_kind(row):
        source = row.get("summary", {}).get("source", {})
        variant = source.get("interaction_variant") or {}
        return variant.get("variant_type", "source")

    kinds = {variant_kind(row) for row in rows}
    if len(kinds) > 1:
        return {
            "version": "tau-real-diagnostics/2",
            "evaluation_log": {"total_trials": len(rows), "records": rows},
            "groups": {
                kind: summarize([r for r in rows if variant_kind(r) == kind])
                for kind in sorted(kinds)
            },
        }

    profiles = {r.get("profile") for r in rows if r.get("profile")}
    if len(profiles) > 1:
        return {
            "version": "tau-real-diagnostics/1",
            "evaluation_log": {"total_trials": len(rows), "records": rows},
            "groups": {
                profile: summarize([r for r in rows if r.get("profile") == profile])
                for profile in sorted(profiles)
            },
        }

    def intent_version(row):
        result = row.get("summary", {}).get("measures", {}).get("intent", {})
        return result.get("version", "tau-real-intent/1") if result else "unscored"

    versions = {intent_version(row) for row in rows} - {"unscored"}
    if len(versions) > 1:
        return {
            "version": "tau-real-diagnostics/1",
            "evaluation_log": {"total_trials": len(rows), "records": rows},
            "groups": {
                version: summarize(
                    [row for row in rows if intent_version(row) == version]
                )
                for version in sorted(versions)
            },
        }
    state_versions = {
        (r.get("summary", {}).get("measures", {}).get("state_interaction") or {}).get(
            "version"
        )
        for r in rows
    } - {None}
    if len(state_versions) > 1:
        return {
            "version": "tau-real-diagnostics/2",
            "evaluation_log": {
                "total_trials": len(rows),
                "state_versions": sorted(state_versions),
            },
            "groups": {
                version: summarize(
                    [
                        r
                        for r in rows
                        if (
                            r.get("summary", {})
                            .get("measures", {})
                            .get("state_interaction")
                            or {}
                        ).get("version")
                        == version
                    ]
                )
                for version in sorted(state_versions)
            },
        }

    def feedback_version(row):
        result = row.get("summary", {}).get("measures", {}).get("conciseness") or {}
        return result.get("version") or (
            "tau-feedback-conciseness/1" if "trial" in result else "unscored"
        )

    feedback_versions = {feedback_version(row) for row in rows} - {"unscored"}
    if len(feedback_versions) > 1:
        buckets = {}
        for row in rows:
            buckets.setdefault(feedback_version(row), []).append(row)
        return {
            "version": "tau-real-diagnostics/2",
            "evaluation_log": {
                "total_trials": len(rows),
                "feedback_versions": sorted(feedback_versions),
            },
            "groups": {
                version: summarize(bucket)
                for version, bucket in sorted(buckets.items())
            },
        }
    # Reuse report grouping so different voice definitions are never pooled.
    voice_profiles = {
        digest({"version": result.get("version"), "config": result.get("config")})
        for row in rows
        if (result := row.get("summary", {}).get("measures", {}).get("voice"))
        and "metrics" in result
    }
    if len(voice_profiles) > 1:
        buckets = {}
        for row in rows:
            result = row.get("summary", {}).get("measures", {}).get("voice") or {}
            key = (
                digest(
                    {"version": result.get("version"), "config": result.get("config")}
                )
                if "metrics" in result
                else "unscored"
            )
            buckets.setdefault(key, []).append(row)
        return {
            "version": "tau-real-diagnostics/2",
            "evaluation_log": {"total_trials": len(rows)},
            "groups": {key: summarize(bucket) for key, bucket in buckets.items()},
        }
    from tau2.metrics.aggregation import task_key, task_mean

    def measures(name):
        return [
            (row, row.get("summary", {}).get("measures", {}).get(name) or {})
            for row in rows
        ]

    def average(name, *path):
        values = []
        for row, result in measures(name):
            if result.get("diagnostic_only"):
                continue
            if name == "intent" and result.get("f1") is None:
                continue
            value = result
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            values.append((task_key(row), value))
        return task_mean(values)

    # Raw evidence, counts and statuses belong to the evaluation log. Only
    # capability values enter the metric panel; no additional judging occurs.
    statuses = {
        name: dict(
            Counter(result.get("status", "unavailable") for _, result in measures(name))
        )
        for name in (
            "intent",
            "execution",
            "voice",
            "interaction",
            "state_interaction",
            "conciseness",
            "persistent_task",
        )
    }
    metrics = {
        "intent": {
            "precision": average("intent", "precision"),
            "recall": average("intent", "recall"),
            "macro_f1": average("intent", "f1"),
        },
        "execution": {
            name: {
                field: average("execution", "dimensions", name, field)
                for field in ("score", "lower", "upper")
            }
            for name in ("tools", "parameters", "order", "effects")
        },
        "interaction": {
            "effective_interaction_rate": average(
                "interaction", "effective_interaction_rate"
            )
        },
        "state_interaction": {
            "progressivity": {
                "forward_rate": average(
                    "state_interaction", "progressivity", "forward_rate"
                )
            },
            "grounding": {
                "consistent_rate": average(
                    "state_interaction", "grounding", "consistent_rate"
                )
            },
        },
        "conciseness": {
            name: average("conciseness", "trial", name)
            for name in (
                "required_coverage",
                "length_efficiency",
                "feedback_score",
            )
        },
    }
    environment_rows = [
        (task_key(row), row.get("env_assertion"))
        for row in rows
        if "env_assertion" in row
    ]
    if environment_rows:
        metrics["environment"] = {"env_assertion": task_mean(environment_rows)}
    # Preserve applicability groups for execution totals. A total with different
    # effective weights must not silently acquire the same meaning.
    groups = {}
    for row, result in measures("execution"):
        if result.get("diagnostic_only") or not result.get("dimensions"):
            continue
        profile = {
            k: {"status": v["status"], "coverage": v["coverage"]}
            for k, v in result["dimensions"].items()
        }
        key = digest(profile)
        groups.setdefault(key, {"profile": profile, "rows": []})["rows"].append(
            (row, result)
        )
    metrics["execution"]["totals_by_profile"] = {
        key: {
            field: task_mean(
                (task_key(row), result.get("total", {}).get(field))
                for row, result in group["rows"]
            )
            for field in ("score", "lower", "upper")
        }
        for key, group in groups.items()
    }
    voice = [
        dict(result, task_key=task_key(row))
        if result and "metrics" in result and not result.get("diagnostic_only")
        else None
        for row, result in measures("voice")
    ]
    voice_report = aggregate_voice(voice)
    namespace = voice_report.get("namespace", "real_voice")
    metrics[namespace] = {
        name: item["value"] for name, item in voice_report["metrics"].items()
    }
    from tau2.persistent_tasks.scores import completion_score

    persistent = []
    hidden = []
    for row in rows:
        result = row.get("summary", {}).get("measures", {}).get("persistent_task")
        if result is None:
            result = (row.get("persistent_tasks") or {}).get("grading", {})
        if result and not result.get("diagnostic_only"):
            persistent.append((row, result))
        if row.get("hidden_intent"):
            hidden.append((row, row["hidden_intent"]))
    metrics["persistent_task"] = {
        "completion_score": task_mean(
            (task_key(row), completion_score(result)) for row, result in persistent
        )
    }
    metrics["hidden_intent"] = {
        "hidden_ask_rate": task_mean(
            (task_key(row), result.get("hidden_ask_rate")) for row, result in hidden
        )
    }
    return {
        "version": "tau-real-diagnostics/2",
        "metrics": metrics,
        "evaluation_log": {
            "total_trials": len(rows),
            "records": rows,
            "statuses": statuses,
            "intent_versions": sorted(versions),
            "feedback_versions": sorted(feedback_versions),
            "execution_profiles": {
                key: group["profile"] for key, group in groups.items()
            },
            "voice": voice_report,
            "termination_reasons": dict(
                Counter(row.get("termination_reason", "unknown") for row in rows)
            ),
            "stage_coverage": {
                name: sum(
                    row.get("summary", {}).get("coverage", {}).get(name, 0)
                    for row in rows
                )
                for name in (
                    "total_stages",
                    "delivered_stages",
                    "fully_delivered_stages",
                )
            },
        },
    }

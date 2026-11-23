"""Authoring-only commands, deliberately separate from tau2 run."""

import argparse
import json
from pathlib import Path

from tau2.config import (
    DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS,
    DEFAULT_GAIA2_FULL_MAX_CONCURRENCY,
    DEFAULT_GAIA2_FULL_START_CONCURRENCY,
    DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY,
    DEFAULT_GAIA2_SERIES_CONCURRENCY,
    DEFAULT_SEED,
)


def add_series_args(parser):
    """Register commands without importing optional Gaia2 applications eagerly."""
    commands = parser.add_subparsers(dest="series_command", required=True)
    light = commands.add_parser(
        "repair-light",
        help="Repair the frozen 399-task queue; keep the 401 passed tasks unchanged",
    )
    light.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Existing completed audit version with repair-queue.json",
    )
    light.add_argument("--output", type=Path, required=True)
    light.add_argument("--config", type=Path, required=True)
    light.add_argument(
        "--concurrency",
        type=int,
        help="Concurrent tasks; reuse saved setting on resume",
    )
    light.add_argument(
        "--per-key-concurrency",
        type=int,
        help="Maximum simultaneous requests per MiMo key; reuse saved setting on resume",
    )
    light.add_argument(
        "--task-limit",
        type=int,
        help="Process only the first N frozen queue tasks in this run",
    )
    light.add_argument(
        "--retry-from",
        type=Path,
        help="Explicitly create a new protocol round from a stopped lightweight run",
    )
    light.set_defaults(func=run_command)
    revalidate = commands.add_parser(
        "revalidate-series",
        help="Source-isolated planning, full legacy audit and bounded rebuild",
    )
    revalidate.add_argument("--dataset", type=Path, required=True)
    revalidate.add_argument("--output", type=Path, required=True)
    revalidate.add_argument("--config", type=Path, required=True)
    revalidate.add_argument(
        "--audit-concurrency",
        type=int,
        default=DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY,
    )
    revalidate.set_defaults(func=run_command)
    audit = commands.add_parser(
        "audit-questions", help="Audit every frozen legacy-reviewed request pair"
    )
    audit.add_argument("--dataset", type=Path, required=True)
    audit.add_argument("--config", type=Path, required=True)
    audit.add_argument(
        "--concurrency", type=int, default=DEFAULT_GAIA2_QUESTION_AUDIT_CONCURRENCY
    )
    audit.set_defaults(func=run_command)
    acceptance = commands.add_parser(
        "repair-acceptance", help="Run one isolated MiMo repair acceptance"
    )
    acceptance.add_argument("--dataset", type=Path, required=True)
    acceptance.add_argument("--output", type=Path, required=True)
    acceptance.add_argument("--task-id", required=True)
    acceptance.add_argument("--config", type=Path, required=True)
    acceptance.add_argument(
        "--content-attempts", type=int, default=DEFAULT_GAIA2_ACCEPTANCE_ATTEMPTS
    )
    acceptance.set_defaults(func=run_command)
    inventory = commands.add_parser(
        "inventory", help="Index and pin all 800 sealed tasks"
    )
    inventory.add_argument(
        "--source", type=Path, default=Path("data/tau2/domains/gaia2")
    )
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--seed", type=int, default=DEFAULT_SEED)
    inventory.set_defaults(func=run_command)
    full = commands.add_parser(
        "full", help="Run resumable 800-task generation, repair and review"
    )
    full.add_argument("--dataset", type=Path, required=True)
    full.add_argument("--config", type=Path, required=True)
    full.add_argument(
        "--start-concurrency", type=int, default=DEFAULT_GAIA2_FULL_START_CONCURRENCY
    )
    full.add_argument(
        "--max-concurrency", type=int, default=DEFAULT_GAIA2_FULL_MAX_CONCURRENCY
    )
    full.add_argument("--request-timeout", type=float, default=600)
    full.add_argument("--provider-attempts", type=int, default=2)
    full.add_argument("--dry-run", action="store_true")
    full.add_argument("--api-key-pool", nargs="+", default=[])
    full.add_argument("--per-key-concurrency", type=int, default=6)
    full.add_argument("--fixed-concurrency", action="store_true")
    full.set_defaults(func=run_command)
    recover = commands.add_parser(
        "recover", help="Repair failed records and rerun verification/review"
    )
    recover.add_argument("--dataset", type=Path, required=True)
    recover.add_argument("--config", type=Path, required=True)
    recover.add_argument("--repair-rounds", type=int, default=1)
    recover.add_argument(
        "--concurrency", type=int, default=DEFAULT_GAIA2_FULL_MAX_CONCURRENCY
    )
    recover.add_argument("--request-timeout", type=float, default=600)
    recover.add_argument("--provider-attempts", type=int, default=2)
    recover.add_argument("--api-key-pool", nargs="+", default=[])
    recover.add_argument("--per-key-concurrency", type=int, default=6)
    recover.add_argument("--fixed-concurrency", action="store_true")
    recover.add_argument("--dry-run", action="store_true")
    recover.set_defaults(func=run_command)
    for name, help_text in [
        (
            "generate",
            "Write and reference-check two continuations per task (resumable)",
        ),
        ("repair", "Repair explicit failed tasks with a persistent extra budget"),
        (
            "verify",
            "Check material, execute references and strictly replay without models",
        ),
        (
            "review",
            "Independently review full candidates and write human review sheets",
        ),
        ("review-sheet", "Write readable tasks and unsigned human decision templates"),
        ("import-review", "Import content-bound human decisions"),
        ("export", "Require 800 accepted series, or explicitly export draft coverage"),
        ("status", "Report all 800 tasks, review coverage and model usage"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--dataset", type=Path, required=True)
        command.set_defaults(func=run_command)
        if name in {"generate", "repair", "verify", "review", "review-sheet"}:
            scope = command.add_mutually_exclusive_group()
            scope.add_argument("--task-ids", nargs="+")
            scope.add_argument(
                "--pilot", action="store_true", help="Fixed 50-task human pilot"
            )
            scope.add_argument("--pilot-per-category", type=int)
        if name in {"generate", "repair", "review"}:
            command.add_argument("--config", type=Path, required=True)
            command.add_argument(
                "--concurrency",
                type=int,
                help="Operational worker override; preserves authored content",
            )
            command.add_argument(
                "--request-timeout",
                type=float,
                help="Seconds allowed per model request",
            )
            command.add_argument(
                "--provider-attempts",
                type=int,
                default=1,
                help="Bounded attempts per transient API failure (1 to 3)",
            )
            command.add_argument(
                "--stop-on-provider-error",
                action="store_true",
                help="Stop the whole batch instead of isolating a failed task",
            )
        if name in {"generate", "repair"}:
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Report scope without model calls",
            )
        if name == "verify":
            command.add_argument(
                "--workers", type=int, default=DEFAULT_GAIA2_SERIES_CONCURRENCY
            )
        if name == "import-review":
            command.add_argument("--decisions", type=Path, required=True)
        if name == "export":
            command.add_argument(
                "--allow-partial",
                action="store_true",
                help="Write explicitly incomplete draft-export only",
            )

    for command in commands.choices.values():
        command.add_argument("--api-config-dir", type=Path)


def run_command(args):
    """Dispatch one command and fail visibly on unsuccessful attempted work."""
    from tau2.task_authoring.gaia2_series import pipeline
    from tau2.task_authoring.gaia2_series.materials import inventory
    from tau2.task_authoring.gaia2_series.schema import AuthoringConfig, RequestPolicy
    from tau2.task_authoring.gaia2_series.storage import read_record, source_tasks

    if getattr(args, "api_config_dir", None) is not None:
        from tau2.task_authoring.gaia2_series.schema import AuthoringConfig

        def load_config():
            value = json.loads(args.config.read_text())
            value["api_config_dir"] = str(args.api_config_dir)
            return AuthoringConfig.model_validate(value)
    else:

        def load_config():
            return AuthoringConfig.model_validate_json(args.config.read_text())

    name = args.series_command
    if name == "repair-light":
        from tau2.task_authoring.gaia2_series.generation import resolve_profile
        from tau2.task_authoring.gaia2_series.lightweight import (
            prepare_scope,
            run_queue,
        )

        config = load_config()
        models = [
            resolve_profile(config, p)[0].casefold()
            for p in [config.author, config.reviewer]
        ]
        if any("mimo" not in model and "deepseek" not in model for model in models):
            raise ValueError(
                "Light repair supports only the configured MiMo or DeepSeek model"
            )
        prepare_scope(args.dataset, args.output, retry_from=args.retry_from)
        with pipeline.dataset_lock(args.output):
            report = run_queue(
                args.output,
                config,
                concurrency=args.concurrency,
                per_key_concurrency=args.per_key_concurrency,
                task_limit=args.task_limit,
            )
    elif name in {"revalidate-series", "audit-questions"}:
        from tau2.task_authoring.gaia2_series.revalidation import (
            audit_population,
            prepare_version,
            run_workflow,
        )

        config = load_config()
        if name == "revalidate-series":
            prepare_version(args.dataset, args.output, config)
            with pipeline.dataset_lock(args.output):
                report = run_workflow(
                    args.output, config, audit_concurrency=args.audit_concurrency
                )
        else:
            with pipeline.dataset_lock(args.dataset):
                report = audit_population(
                    args.dataset, config, concurrency=args.concurrency
                )
    elif name == "repair-acceptance":
        from tau2.task_authoring.gaia2_series.acceptance import run_acceptance

        report = run_acceptance(
            args.dataset,
            args.output,
            args.task_id,
            load_config(),
            content_attempts=args.content_attempts,
        )
    elif name == "inventory":
        source, output = args.source.resolve(), args.output.resolve()
        if source.is_relative_to(output) or output.is_relative_to(source):
            raise ValueError("Authoring output must be separate from the sealed source")
        args.output.mkdir(parents=True, exist_ok=True)
        with pipeline.dataset_lock(args.output):
            report = inventory(args.source, args.output, args.seed)
    elif name == "full":
        from tau2.task_authoring.gaia2_series.full import preflight, run_full

        config = load_config()
        with pipeline.dataset_lock(args.dataset):
            if args.dry_run:
                report = preflight(args.dataset, config)
            else:
                report = run_full(
                    args.dataset,
                    config,
                    start_concurrency=args.start_concurrency,
                    max_concurrency=args.max_concurrency,
                    request_policy=RequestPolicy(
                        timeout_seconds=args.request_timeout,
                        attempts=args.provider_attempts,
                        api_key_pool=args.api_key_pool,
                        per_key_concurrency=args.per_key_concurrency,
                        adaptive_concurrency=not args.fixed_concurrency,
                    ),
                )
    elif name == "recover":
        from tau2.task_authoring.gaia2_series.recovery import (
            recovery_scope,
            run_recovery,
        )

        config = load_config()
        with pipeline.dataset_lock(args.dataset):
            scope = recovery_scope(args.dataset)
            if args.dry_run:
                report = {
                    "scope": scope,
                    "repair_rounds": args.repair_rounds,
                    "model_calls": 0,
                    "release": False,
                }
            else:
                report = run_recovery(
                    args.dataset,
                    config,
                    concurrency=args.concurrency,
                    repair_rounds=args.repair_rounds,
                    request_policy=RequestPolicy(
                        timeout_seconds=args.request_timeout,
                        attempts=args.provider_attempts,
                        api_key_pool=args.api_key_pool,
                        per_key_concurrency=args.per_key_concurrency,
                        adaptive_concurrency=not args.fixed_concurrency,
                    ),
                )
    else:
        manifest, tasks = source_tasks(args.dataset)
        if name == "status":
            records = [read_record(args.dataset, task_id) for task_id in sorted(tasks)]
            states = {}
            calls = [call for record in records for call in record.get("calls", [])]
            for record in records:
                status = record.get("status", "pending")
                states[status] = states.get(status, 0) + 1
            required = pipeline.human_review_ids(args.dataset, manifest, tasks)
            report = {
                "expected_tasks": 800,
                "status": states,
                "required_human_reviews": len(required),
                "completed_human_reviews": sum(
                    record.get("human", {}).get("decision") == "accepted"
                    for record in records
                    if record["base_task_id"] in required
                ),
                "model_calls": len(calls),
                "known_cost": sum(call.get("cost") or 0 for call in calls),
                "calls_without_cost": sum(call.get("cost") is None for call in calls),
                "execution_ready": False,
            }
            for filename, key in [
                ("full-run.json", "full_run"),
                ("progress.json", "progress"),
            ]:
                path = args.dataset / filename
                if path.exists():
                    report[key] = json.loads(path.read_text())
        else:
            with pipeline.dataset_lock(args.dataset):
                if name == "import-review":
                    report = pipeline.import_reviews(args.dataset, args.decisions)
                elif name == "export":
                    report = pipeline.export_dataset(
                        args.dataset, allow_partial=args.allow_partial
                    )
                else:
                    if name == "repair" and not args.task_ids:
                        raise ValueError("repair requires explicit --task-ids")
                    ids = pipeline.selected_ids(
                        manifest,
                        tasks,
                        ids=args.task_ids,
                        pilot=args.pilot,
                        per_category=args.pilot_per_category,
                    )
                    if name in {"generate", "repair", "review"}:
                        config = AuthoringConfig.model_validate_json(
                            args.config.read_text()
                        )
                        if name in {"generate", "repair"} and args.dry_run:
                            report = {
                                "expected_tasks": 800,
                                "selected": len(ids),
                                "task_ids": ids,
                                "new_stages_per_task": 2,
                                "model_calls": 0,
                            }
                        else:
                            function = (
                                pipeline.generate_dataset
                                if name in {"generate", "repair"}
                                else pipeline.review_dataset
                            )
                            extra = {"repair": True} if name == "repair" else {}
                            report = function(
                                args.dataset,
                                config,
                                ids,
                                concurrency=args.concurrency,
                                request_policy=RequestPolicy(
                                    timeout_seconds=args.request_timeout,
                                    attempts=args.provider_attempts,
                                    stop_batch_on_error=args.stop_on_provider_error,
                                ),
                                **extra,
                            )
                    elif name == "verify":
                        if args.workers < 1:
                            raise ValueError("workers must be positive")
                        report = pipeline.verify_dataset(
                            args.dataset, ids, args.workers
                        )
                    else:
                        pipeline.review_sheet(args.dataset, ids)
                        report = {
                            "selected": len(ids),
                            "sheet": str(args.dataset / "review.md"),
                        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if name == "repair-light" and not report.get("complete"):
        raise SystemExit(1)
    if name in {"revalidate-series", "audit-questions"} and not report.get("complete"):
        raise SystemExit(1)
    if name == "repair-acceptance" and not report.get("scope_complete"):
        raise SystemExit(1)
    if name == "full" and report.get("status") == "needs_attention":
        raise SystemExit(1)
    if "results" in report:
        successful = {"verified", "reviewed"}
        if any(result["status"] not in successful for result in report["results"]):
            raise SystemExit(1)


def main():
    """Support a lightweight module entry point as well as the tau2 CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_series_args(parser)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

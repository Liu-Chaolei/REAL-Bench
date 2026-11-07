"""CLI for independent REAL-derived metrics on ordinary saved tau runs."""

import json
from pathlib import Path

from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.io import write_json


def add_metrics_args(parser):
    """Register explicit evaluation and read-only report commands."""
    commands = parser.add_subparsers(dest="metrics_command", required=True)
    for name in ("run", "summarize", "export"):
        child = commands.add_parser(name)
        child.add_argument("results", type=Path)
        child.add_argument("--output", type=Path)
        if name == "run":
            child.add_argument("--config", type=Path, required=True)
            child.add_argument("--only", help="Comma-separated intent,execution,voice")
            child.add_argument("--dry-run", action="store_true")
        child.set_defaults(func=run_metrics)

    for name in ("coverage", "verify-gaia2"):
        child = commands.add_parser(name)
        child.add_argument("--output", type=Path, required=True)
        if name == "verify-gaia2":
            child.add_argument("--workers", type=int, default=4)
        child.set_defaults(func=run_validation)


def run_validation(args):
    """Inspect all native Gaia2 rules, optionally execute/replay references."""
    from tau2.domains.gaia2.utils import GAIA2_DATA_DIR

    if args.metrics_command == "coverage":
        from tau2.diagnostics.coverage import scan

        report = scan(GAIA2_DATA_DIR)
        write_json(args.output, report)
        print(
            json.dumps(
                {
                    "tasks": report["tasks"],
                    "complete": report["complete"],
                    "missing": report["missing"],
                }
            )
        )
    else:
        from tau2.diagnostics.verify import verify

        report = verify(GAIA2_DATA_DIR, args.output, workers=args.workers)
        print(json.dumps({k: v for k, v in report.items() if k != "records"}))


def run_metrics(args):
    """Evaluation is opt-in; summarize/export never invoke a model."""
    if args.metrics_command == "run":
        from tau2.diagnostics.suite import run_saved

        only = args.only.split(",") if args.only else None
        if only and any(x not in ("intent", "execution", "voice") for x in only):
            raise ValueError("--only accepts intent,execution,voice")
        report = run_saved(
            args.results, MetricsConfig.load(args.config), only, args.dry_run
        )
    else:
        from tau2.diagnostics.suite import read_saved_reports

        report = read_saved_reports(args.results)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

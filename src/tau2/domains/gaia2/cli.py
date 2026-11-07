"""Data preparation commands; evaluations use the ordinary tau2 run command."""

import json
from pathlib import Path

from tau2.domains.gaia2.utils import GAIA2_DATA_DIR, GAIA2_VERSION


def publish_binding(root: Path):
    """Inherit English personas through the existing language factory."""
    from tau2.domains.gaia2.environment import get_tasks
    from tau2.domains.gaia2.importer import USER_CONSTRAINTS
    from tau2.multilingual.factory.pack_extension import publish_registry_extension

    if Path(root).resolve() != GAIA2_DATA_DIR.resolve():
        return None
    tasks = get_tasks(root=root)
    metadata = {}
    for task in tasks:
        known = json.loads(task.user_scenario.instructions.known_info or "{}")
        gender = str(known.get("gender", "")).lower()
        metadata[task.id] = {
            "canonical_task_id": task.id,
            "caller_gender": gender if gender in {"male", "female"} else None,
        }
    return publish_registry_extension(
        name="gaia2",
        language="en",
        domain="gaia2",
        revision=GAIA2_VERSION,
        metadata=metadata,
        user_guidelines=USER_CONSTRAINTS,
    )


def add_gaia2_args(parser):
    """Register import and validation commands without a second run pipeline."""
    commands = parser.add_subparsers(dest="gaia2_command", required=True)
    importing = commands.add_parser(
        "import", help="Convert five local Parquet configurations"
    )
    importing.add_argument("--source", type=Path, required=True)
    importing.add_argument("--assets", type=Path)
    importing.add_argument("--output", type=Path, default=GAIA2_DATA_DIR)
    importing.add_argument("--replace-incomplete", action="store_true")

    def run_import(args):
        from tau2.domains.gaia2.importer import import_dataset

        result = import_dataset(
            args.source,
            output=args.output,
            assets=args.assets,
            replace_incomplete=args.replace_incomplete,
        )
        publish_binding(args.output)
        print(json.dumps(result, indent=2))

    importing.set_defaults(func=run_import)
    verifying = commands.add_parser(
        "verify", help="Validate task, resource and operation contracts"
    )
    verifying.add_argument("--data", type=Path, default=GAIA2_DATA_DIR)
    verifying.add_argument(
        "--reference",
        action="store_true",
        help="Execute all reference operations without model calls",
    )
    verifying.add_argument("--workers", type=int, default=4)
    verifying.add_argument(
        "--seal", action="store_true", help="Pin a verified import for evaluation"
    )

    def run_verify(args):
        from tau2.domains.gaia2.verify import verify_dataset

        result = verify_dataset(
            args.data, reference=args.reference, workers=args.workers, seal=args.seal
        )
        print(json.dumps(result, indent=2))
        if not result["valid"]:
            raise SystemExit(1)

    verifying.set_defaults(func=run_verify)

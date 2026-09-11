"""Audio preflight and task generation, shared by the CLI and tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from adapters.gaia2.adapter import Gaia2Adapter
from adapters.gaia2.audio import find_asset, read_manifest
from adapters.gaia2.qwen_audio_tasks import generate_audio_task, stage_qwen
from adapters.gaia2.run_adapter import _load_requested_ids, _matches_requested_ids
from datasets import load_dataset


def generate(args) -> None:
    if args.audio_dir is None and args.audio_manifest is None:
        raise ValueError("--audio-dir or --audio-manifest is required")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit must be positive")
    if not args.preflight and args.qwen_agent_dir is None:
        raise ValueError("--qwen-agent-dir is required when generating tasks")
    output = Path(args.output_dir)
    if args.cleanup and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    requested = _load_requested_ids(args.ids, args.ids_file)
    manifest = read_manifest(args.audio_manifest) if args.audio_manifest else {}
    eligible, excluded, seen = [], [], set()
    for config in args.configs:
        if args.dataset_dir:
            data = load_dataset(
                "parquet",
                data_files={
                    args.split: str(
                        args.dataset_dir / config / f"{args.split}-*.parquet"
                    ),
                },
                split=args.split,
            )
        else:
            data = load_dataset(
                args.dataset_name, name=config, split=args.split, revision=args.revision
            )
        for record in data:
            if not _matches_requested_ids(record, requested):
                continue
            task = Gaia2Adapter.metadata_from_record(
                record=dict(record), config=config, split=args.split
            )
            seen.update((task.source_id, task.scenario_id))
            try:
                asset = find_asset(task, audio_dir=args.audio_dir, manifest=manifest)
            except (ValueError, OSError) as exc:
                excluded.append(
                    {"config": config, "source_id": task.source_id, "reason": str(exc)}
                )
            else:
                eligible.append((task, asset))
    if requested and requested - seen:
        raise ValueError(f"Unknown requested IDs: {sorted(requested - seen)}")
    smoke = [
        min(
            (pair for pair in eligible if pair[0].config == config),
            key=lambda pair: (pair[1].duration_sec, pair[0].source_id),
        )
        for config in args.configs
        if any(t.config == config for t, _ in eligible)
    ]
    selected = smoke if args.smoke else eligible
    if args.limit is not None:
        selected = selected[: args.limit]
    report = {
        "benchmark": "gaia2-qwen-audio",
        "eligible": len(eligible),
        "selected": len(selected),
        "by_config": {c: sum(t.config == c for t, _ in eligible) for c in args.configs},
        "excluded": excluded,
    }
    (output / "coverage.json").write_text(json.dumps(report, indent=2) + "\n")
    (output / "audio-index.jsonl").write_text(
        "".join(json.dumps(a.to_dict()) + "\n" for _, a in eligible)
    )
    (output / "selected-ids.txt").write_text(
        "".join(t.source_id + "\n" for t, _ in selected)
    )
    (output / "smoke-ids.txt").write_text("".join(t.source_id + "\n" for t, _ in smoke))
    if not args.preflight:
        source = stage_qwen(args.qwen_agent_dir, output)
        adapter = Gaia2Adapter(output)
        for task, asset in selected:
            generate_audio_task(adapter, task, asset, source, overwrite=args.overwrite)
    print(json.dumps(report, indent=2))

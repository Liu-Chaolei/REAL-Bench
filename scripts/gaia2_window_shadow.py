"""Read-only historical v4 projection diagnostics; never publishes a new score."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from adapters.gaia2.qwen_audio_template.environment.runner.observation import (
    eligibility,
    project_audio,
)
from adapters.gaia2.window_audit import verify_integrity


def shadow(campaign, output, count=15):
    if output.exists():
        raise ValueError("Preserve existing shadow analysis; choose a new output")
    ledger = json.loads((campaign / "execution-ledger.json").read_text())
    if len(ledger["results"]) < count or count <= 0:
        raise ValueError("Requested complete first-attempt scope is unavailable")
    manifest = json.loads((campaign / "manifest.json").read_text())
    index = {r["source_id"]: r for r in manifest["seed_cases"]}
    rows: list[dict[str, Any]] = []
    for entry in ledger["results"][:count]:
        root = next(Path(entry["job"]).glob("*/artifacts/evaluation/results"))
        files = verify_integrity(root)
        projected = project_audio(root)
        scenario = (
            campaign
            / "dataset"
            / index[entry["source_id"]]["task_name"]
            / "environment/scenario.json"
        )
        rows.append(
            {
                "source_id": entry["source_id"],
                "supported": eligibility(json.loads(scenario.read_text())),
                "original_success": entry["result"].get("success"),
                "new_protocol_score": None,
                "verified_artifact_count": files,
                "projection": projected,
                "observation_end_validation": "unknown: original history lacks complete v4 activity snapshots and may end before the idle window",
            }
        )
    report = {
        "mode": "historical_shadow_only",
        "primary_results_modified": False,
        "new_model_calls": 0,
        "count": count,
        "complete_audible_projections": sum(
            r["projection"]["audible_transcript_complete"] for r in rows
        ),
        "incomplete_audible_projections": sum(
            not r["projection"]["audible_transcript_complete"] for r in rows
        ),
        "new_protocol_scores_available": False,
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=15)
    args = parser.parse_args()
    report = shadow(args.campaign, args.output, args.count)
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))


if __name__ == "__main__":
    main()

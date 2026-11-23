"""Run three deterministic fixture examples for the probe."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from propbank_probe.cli import main  # noqa: E402


if __name__ == "__main__":
    output = ROOT / "results" / "smoke"
    main(["parse", "--input", str(ROOT / "data" / "smoke_cases.jsonl"), "--backend", "fixture", "--output", str(output)])
    main(["map", "--input", str(output / "normalized.jsonl"), "--mapping-config", str(ROOT / "data" / "gaia_mapping.yaml"), "--output", str(output)])
    main(
        [
            "report",
            "--predictions",
            str(output / "normalized.jsonl"),
            "--mapped",
            str(output / "mapped.jsonl"),
            "--composition",
            str(output / "composition.jsonl"),
            "--gold",
            str(ROOT / "data" / "smoke_expected.jsonl"),
            "--output",
            str(output / "report.md"),
        ]
    )


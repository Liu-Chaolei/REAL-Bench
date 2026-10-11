"""Select reproducible paired audio assets whose scenarios pass v8 eligibility."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

from adapters.gaia2.qwen_audio_template.environment.runner.observation import eligibility


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paired", type=Path, required=True)
    parser.add_argument("--dataset-snapshot", type=Path, required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261006)
    args = parser.parse_args()

    records = {}
    for config in ("adaptability", "ambiguity", "execution", "search", "time"):
        parquet = next((args.dataset_snapshot / config).glob("validation-*.parquet"))
        for row in pq.read_table(parquet, columns=["id", "data"]).to_pylist():
            records[(config, row["id"])] = json.loads(row["data"])

    supported = []
    for line in args.paired.read_text(encoding="utf-8").splitlines():
        config, source_id, full, target = line.split("\t")
        scenario = records.get((config, source_id))
        if scenario is not None and eligibility(scenario)["supported"]:
            supported.append((config, source_id, full, target))

    if len(supported) < args.count:
        raise ValueError(f"only {len(supported)} v8-supported pairs available")
    chosen = random.Random(args.seed).sample(supported, args.count)
    print(json.dumps({"supported": len(supported), "by_config": dict(Counter(r[0] for r in supported))}))
    for row in chosen:
        print("\t".join(row))


if __name__ == "__main__":
    main()

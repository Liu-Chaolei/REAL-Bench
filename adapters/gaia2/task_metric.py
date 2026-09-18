# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Harbor metric for v5: empty task rewards are unknown, not zero."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def compute(rewards):
    scores = [r["reward"] for r in rewards if r and "reward" in r]
    if any(type(score) not in {int, float} or score not in {0, 1} for score in scores):
        raise ValueError("Expected binary official task rewards")
    result = {
        "scored_count": len(scores),
        "unresolved_count": sum(r == {} for r in rewards),
        "unavailable_count": sum(r is None for r in rewards),
        "score_coverage": len(scores) / len(rewards) if rewards else 0.0,
    }
    if scores:
        result["task_pass_rate"] = sum(scores) / len(scores)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", type=Path, required=True)
    parser.add_argument("-o", type=Path, required=True)
    args = parser.parse_args()
    rewards = [json.loads(line) for line in args.i.read_text().splitlines()]
    args.o.write_text(json.dumps(compute(rewards)) + "\n")


if __name__ == "__main__":
    main()

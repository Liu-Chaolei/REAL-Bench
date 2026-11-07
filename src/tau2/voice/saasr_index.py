"""Build a generic domain/task -> SAASR scene index without editing scene data."""

import argparse
import json
import os
from pathlib import Path


def build_index(root: Path, domain: str, output: Path) -> dict[str, str]:
    """Index category/sample/generated_scene.yaml using sample as canonical task ID."""
    root, output = Path(root).resolve(), Path(output).resolve()
    index = {}
    for path in sorted(root.glob("*/*/generated_scene.yaml")):
        key = f"{domain}/{path.parent.name}"
        if key in index:
            raise ValueError(f"Duplicate task scene: {key}")
        index[key] = os.path.relpath(path, output.parent)
    if not index:
        raise ValueError(f"No scenes under {root}")
    return index


def main():
    """Write an explicit, reviewable scene map."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    index = build_index(args.root, args.domain, args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(index, indent=2) + "\n")
    print(f"Indexed {len(index)} scenes in {args.output}")


if __name__ == "__main__":
    main()

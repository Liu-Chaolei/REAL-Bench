"""Create a clean full authoring dataset from explicitly approved pilot tasks."""

import argparse
from pathlib import Path


def main():
    """Use the same pinned-runtime validation as the full authoring command."""
    from tau2.task_authoring.gaia2_series.full import prepare_dataset

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    args = parser.parse_args()
    print(prepare_dataset(args.pilot, args.output, args.decisions))


if __name__ == "__main__":
    main()

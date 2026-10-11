"""Expand generated Qwen audio tasks into mixed/clean paired variants."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from adapters.gaia2.audio import inspect_audio, normalize_audio


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    manifest = {
        (row["config"], row["source_id"]): row
        for row in (
            json.loads(line)
            for line in args.manifest.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    }
    task_dirs = sorted(
        path
        for path in args.dataset.iterdir()
        if path.is_dir() and path.name != ".qwen-source"
    )
    if len(task_dirs) != len(manifest):
        raise ValueError(f"expected {len(manifest)} generated tasks, found {len(task_dirs)}")

    for original in task_dirs:
        metadata = json.loads((original / "task_metadata.json").read_text())
        key = (metadata["config"], metadata["source_id"])
        row = manifest[key]
        target = Path(row["audio_path"]).parent.parent.parent / "target_tts.wav"
        if not target.is_file():
            raise FileNotFoundError(target)

        mixed = original.with_name(original.name + "-mixed")
        clean = original.with_name(original.name + "-target-tts")
        original.rename(mixed)
        shutil.copytree(mixed, clean)

        for variant_dir, source, variant in (
            (mixed, Path(row["audio_path"]), "mixed"),
            (clean, target, "target_tts"),
        ):
            normalize_audio(source, variant_dir / "environment/input.wav")
            audio_path = variant_dir / "environment/audio.json"
            audio = json.loads(audio_path.read_text())
            audio.update(inspect_audio(variant_dir / "environment/input.wav"))
            audio["audio_path"] = str(source.resolve())
            audio["variant"] = variant
            audio["pair_id"] = metadata["source_id"]
            audio_path.write_text(json.dumps(audio, indent=2) + "\n")

            metadata_variant = dict(metadata)
            metadata_variant["audio_variant"] = variant
            metadata_variant["pair_id"] = metadata["source_id"]
            (variant_dir / "task_metadata.json").write_text(
                json.dumps(metadata_variant, indent=2, ensure_ascii=False) + "\n"
            )

    (args.dataset / "pairing.json").write_text(
        json.dumps(
            {
                "pair_count": len(manifest),
                "task_count": len(manifest) * 2,
                "variants": ["mixed", "target_tts"],
                "manifest": str(args.manifest.resolve()),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

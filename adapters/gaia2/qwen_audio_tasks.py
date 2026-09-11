"""Package local Qwen sources and external audio as isolated Harbor tasks."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from adapters.gaia2.audio import AudioAsset, normalize_audio

TEMPLATE = Path(__file__).parent / "qwen_audio_template"


def source_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def stage_qwen(root: Path, output: Path) -> Path:
    if not (root / "server/src/core/evaluation.mjs").is_file():
        raise ValueError("Qwen checkout does not include the evaluation bridge")
    target = output / ".qwen-source"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    # Explicit source allowlist prevents personal state, .env and node_modules
    # from being copied into images. Keep workspaces for npm ci's lockfile.
    for name in (
        "package.json",
        "package-lock.json",
        "server",
        "shared",
        "scripts",
        "config",
        "web",
        "cli",
        "tui",
        "desktop",
    ):
        source = root / name
        if source.is_dir():
            shutil.copytree(
                source,
                target / name,
                ignore=shutil.ignore_patterns(
                    "node_modules",
                    ".git",
                    ".env",
                    ".env.*",
                    "runtime",
                    "dist",
                    "*.log",
                ),
            )
        else:
            shutil.copy2(source, target / name)
    shutil.copy2(TEMPLATE / "environment/Dockerfile", target / "Dockerfile.harbor")
    snapshot_hash = hashlib.sha256()
    for path in sorted(target.rglob("*")):
        if path.is_file():
            snapshot_hash.update(str(path.relative_to(target)).encode())
            snapshot_hash.update(path.read_bytes())
    (target / "revision.json").write_text(
        json.dumps(
            {
                "git_revision": source_revision(root),
                "source_sha256": snapshot_hash.hexdigest(),
                "package_version": json.loads((root / "package.json").read_text())[
                    "version"
                ],
            }
        )
    )
    return target.resolve()


def generate_audio_task(
    adapter, task, asset: AudioAsset, source: Path, *, overwrite=False
):
    task_dir = adapter.output_dir / task.local_task_id
    if task_dir.exists():
        if not overwrite:
            return None
        shutil.rmtree(task_dir)
    shutil.copytree(TEMPLATE, task_dir)
    env = task_dir / "environment"
    (env / "scenario.json").write_text(task.scenario_json)
    normalize_audio(Path(asset.audio_path), env / "input.wav")
    (env / "audio.json").write_text(json.dumps(asset.to_dict(), indent=2))
    replacements = adapter._make_replacements(task)
    task_toml = task_dir / "task.toml"
    task_toml.write_text(adapter._render_template(task_toml, replacements))
    compose = env / "docker-compose.yaml"
    # JSON strings are also YAML strings. Escape '$' for Compose interpolation.
    compose.write_text(
        compose.read_text().replace(
            "__QWEN_SOURCE__",
            json.dumps(str(source).replace("$", "$$")),
        )
    )
    adapter._write_metadata(task_dir, task)
    (task_dir / "tests/test.sh").chmod(0o755)
    return task_dir

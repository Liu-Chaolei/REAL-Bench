"""Validate external speech assets without exposing task text to the agent."""

from __future__ import annotations

import hashlib
import json
import subprocess
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AudioAsset:
    config: str
    source_id: str
    event_id: str
    audio_path: str
    duration_sec: float
    sample_rate: int
    channels: int
    sample_width: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_audio(path: Path) -> dict[str, Any]:
    """Read every frame to detect truncated WAVs before spending model calls."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            if not frames or rate <= 0 or audio.getcomptype() != "NONE":
                raise ValueError(f"Empty or compressed WAV: {path}")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError(f"Truncated WAV: {path}")
    except wave.Error as exc:
        raise ValueError(f"Invalid PCM WAV {path}: {exc}") from exc
    return {
        "duration_sec": frames / rate,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "sha256": digest,
    }


def normalize_audio(source: Path, destination: Path) -> None:
    info = inspect_audio(source)
    if (info["sample_rate"], info["channels"], info["sample_width"]) == (16000, 1, 2):
        destination.write_bytes(source.read_bytes())
        return
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
    )
    inspect_audio(destination)


def read_manifest(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    entries = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (str(row["config"]), str(row["source_id"]))
        if key in entries:
            raise ValueError(f"Duplicate audio mapping: {key}")
        audio_path = Path(row["audio_path"])
        row["audio_path"] = str(
            audio_path if audio_path.is_absolute() else path.parent / audio_path
        )
        entries[key] = row
    return entries


def find_asset(
    task, *, audio_dir: Path | None, manifest: dict[tuple[str, str], dict[str, Any]]
) -> AudioAsset:
    scenario = json.loads(task.scenario_json)
    users = [e for e in scenario["events"] if e.get("event_type") == "USER"]
    if len(users) != 1:
        raise ValueError("unsupported_user_message_count")
    event_id = str(users[0]["event_id"])
    row = manifest.get((task.config, task.source_id))
    if audio_dir is not None:
        path = audio_dir / task.config / f"{task.source_id}.wav"
    elif row is not None:
        path = Path(row["audio_path"])
        if row.get("event_id", event_id) != event_id:
            raise ValueError("audio_event_id_mismatch")
    else:
        raise FileNotFoundError("missing_audio_mapping")
    if not path.is_file():
        raise FileNotFoundError("missing_audio")
    return AudioAsset(
        config=task.config,
        source_id=task.source_id,
        event_id=event_id,
        audio_path=str(path.resolve()),
        **inspect_audio(path),
    )

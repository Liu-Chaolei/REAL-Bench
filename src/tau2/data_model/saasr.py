"""Optional caller scene renderer configuration, independent of Agent providers."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau2.config import (
    DEFAULT_SAASR_PREPARE_TIMEOUT_SECONDS,
    DEFAULT_SAASR_REQUEST_TIMEOUT_SECONDS,
)


def file_sha256(path: Path) -> str:
    """Hash file bytes without loading model weights into memory."""
    path = Path(path)
    stat = path.stat()
    return _file_sha256(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=16384)
def _file_sha256(path: str, size: int, modified_ns: int) -> str:
    """Reuse verified immutable input bytes across trials, invalidating on change."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SaasrConfig(BaseModel):
    """Launch independent local GPU workers and resolve task-owned scene files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_commands: list[list[str]] = Field(min_length=1)
    devices: list[str] = Field(min_length=1)
    model_dir: Path
    scene_yaml: Path | None = None
    scene_index: Path | None = None
    request_timeout_seconds: float = Field(
        default=DEFAULT_SAASR_REQUEST_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False
    )
    prepare_timeout_seconds: float = Field(
        default=DEFAULT_SAASR_PREPARE_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_sources(self):
        """Choose one scene source and explicitly assign one GPU per worker."""
        if (self.scene_yaml is None) == (self.scene_index is None):
            raise ValueError("Set exactly one of scene_yaml or scene_index")
        if len(self.devices) != len(self.worker_commands):
            raise ValueError("devices must assign one device to each worker command")
        if any(
            not command or any(not arg for arg in command)
            for command in self.worker_commands
        ):
            raise ValueError("worker_commands must contain nonempty argument arrays")
        return self

    @classmethod
    def from_file(cls, path: Path):
        """Resolve data paths relative to the runtime configuration file."""
        path = Path(path).resolve()
        data = json.loads(path.read_text())
        for field in ("model_dir", "scene_yaml", "scene_index"):
            if data.get(field):
                data[field] = str((path.parent / data[field]).resolve())
        return cls.model_validate(data)

    def scene_for(self, domain: str, task_id: str) -> Path:
        """Resolve a scene generically; no benchmark-specific filename parsing."""
        if self.scene_yaml is not None:
            path = self.scene_yaml
        else:
            index = json.loads(self.scene_index.read_text())
            key = f"{domain}/{task_id}"
            if key not in index:
                raise ValueError(f"No SAASR scene configured for {key}")
            path = self.scene_index.parent / index[key]
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"SAASR scene does not exist: {path}")
        return path

    def preflight(self, domain: str, task_ids: list[str]) -> dict:
        """Check selected resources and freeze their identities before Agent startup."""
        if not self.model_dir.is_dir():
            raise ValueError(f"SAASR model directory does not exist: {self.model_dir}")
        model_files = sorted(
            p
            for p in self.model_dir.rglob("*")
            if p.is_file() and ".cache" not in p.parts and p.suffix != ".lock"
        )
        weights = [p for p in model_files if p.suffix in {".safetensors", ".bin"}]
        if not weights:
            raise ValueError("SAASR model directory contains no weights")
        assets, scenes, missing = {}, {}, []
        for task_id in task_ids:
            try:
                path = self.scene_for(domain, task_id)
            except ValueError as error:
                missing.append(str(error))
                continue
            raw = yaml.safe_load(path.read_text())
            if len(raw.get("scenes", [])) != 1 or not raw["scenes"][0].get(
                "mainspeaker"
            ):
                raise ValueError(f"SAASR needs one mainspeaker scene: {path}")
            scene = raw["scenes"][0]
            refs = raw["speech"]["speaker_references"]
            used = scene.get("speaker_refs", list(range(scene["speakers"])))
            if len(used) != scene["speakers"] or len(set(used)) != len(used):
                raise ValueError(f"Invalid SAASR speaker mapping: {path}")
            resources = [path, path.parent / scene["maindialogue"]["dialogue"]]
            resources += [path.parent / d["dialogue"] for d in scene["dialogues"]]
            for index in used:
                if not 0 <= index < len(refs) or not refs[index]:
                    raise ValueError(f"Missing SAASR speaker reference: {path}")
                reference = (path.parent / refs[index]).resolve()
                resources.append(reference)
                # Both transcript layouts used by Qwen3TTSBackend.
                transcripts = [reference.with_suffix(".txt")]
                if len(reference.parents) >= 3:
                    transcripts.append(
                        reference.parents[2]
                        / "txt"
                        / reference.parent.name
                        / f"{reference.stem}.txt"
                    )
                for transcript in transcripts:
                    if transcript.is_file():
                        resources.append(transcript)
            if raw["audio"].get("noise") == "dataset":
                noise = raw["audio"].get("noise_file")
                if not noise:
                    raise ValueError("Continuous dataset scenes must pin noise_file")
                resources.append(path.parent / noise)
            identities = {}
            for resource in resources:
                resource = resource.resolve()
                key = str(resource)
                if key not in assets:
                    assets[key] = file_sha256(resource)
                identities[key] = assets[key]
            scenes[task_id] = hashlib.sha256(
                json.dumps(identities, sort_keys=True).encode()
            ).hexdigest()
        if missing:
            raise ValueError("Missing SAASR scenes:\n" + "\n".join(missing))
        model = {
            str(p.relative_to(self.model_dir)): file_sha256(p) for p in model_files
        }
        package = Path(__file__).resolve().parents[1]
        renderer_sources = {
            name: file_sha256(package / name)
            for name in (
                "config.py",
                "data_model/run_config.py",
                "data_model/saasr.py",
                "voice/saasr.py",
                "voice/saasr_worker.py",
                "runner/build.py",
                "user/user_simulator_streaming.py",
                "orchestrator/full_duplex_orchestrator.py",
            )
        }
        return {
            "version": "saasr-continuous-v1",
            "config": self.model_dump(mode="json"),
            "scenes": scenes,
            "assets": assets,
            "renderer_sources": renderer_sources,
            "model_sha256": hashlib.sha256(
                json.dumps(model, sort_keys=True).encode()
            ).hexdigest(),
        }

"""Content-addressed Gaia2 inputs and isolated runtime resource locations."""

import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from tau2.utils.utils import DATA_DIR

GAIA2_DATA_DIR = Path(DATA_DIR) / "tau2" / "domains" / "gaia2"
GAIA2_TASKS_PATH = GAIA2_DATA_DIR / "tasks.json"
GAIA2_VERSION = "gaia2-tau-v1"


@lru_cache(maxsize=8)
def _task_artifact_index(path: str, size: int, modified_ns: int, expected: str):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Gaia2 task artifact differs from its sealed manifest")
    payload = json.loads(raw)
    index = {
        task["id"]: hashlib.sha256(canonical_json(task)).hexdigest() for task in payload
    }
    if len(index) != len(payload):
        raise ValueError("Duplicate Gaia2 task identifiers in sealed artifact")
    return index


def verify_task_artifact(root: Path, task=None) -> None:
    """Reject changed task bytes or a substituted task before model startup."""
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    path = (root / "tasks.json").resolve()
    stat = path.stat()
    index = _task_artifact_index(
        str(path), stat.st_size, stat.st_mtime_ns, manifest["tasks_sha256"]
    )
    if task is not None:
        from tau2.domains.gaia2.interaction_variants import apply_variant, variant_for

        variant = variant_for(task)
        if variant is not None:
            from tau2.data_model.tasks import Task

            published = json.loads((root / "interaction_variants.json").read_text())
            if not any(row == variant.model_dump(mode="json") for row in published):
                raise ValueError("Interaction variant is absent from its delta dataset")

            source = next(
                row
                for row in json.loads(path.read_text())
                if row["id"] == variant.source_task_id
            )
            expected = apply_variant(Task.model_validate(source), variant)
            if expected != task:
                raise ValueError("Interaction variant differs from its source view")
            return
    if (
        task is not None
        and index.get(task.id)
        != hashlib.sha256(canonical_json(task.model_dump(mode="json"))).hexdigest()
    ):
        raise ValueError("Gaia2 task differs from the sealed task definition")


def runtime_digest() -> str:
    """Pin the imported applications and shared execution/scoring semantics."""
    package = Path(__file__).resolve().parents[2]
    paths = list(Path(__file__).parent.rglob("*.py"))
    paths.extend(
        package / relative
        for relative in [
            "data_model/scenario.py",
            "data_model/dynamic_followup.py",
            "data_model/diagnostics.py",
            "diagnostics/collect.py",
            "agent/discrete_time_audio_native_agent.py",
            "voice/audio_native/qwen_agent/provider.py",
            "voice/audio_native/qwen_agent/config.py",
            "voice/audio_native/qwen_agent/discrete_time_adapter.py",
            "voice/audio_native/adapter.py",
            "voice/audio_native/tick_result.py",
            "utils/transcript/live.py",
            "utils/transcript/views.py",
            "utils/transcript/audio_text.py",
            "data_model/transcript.py",
            "data_model/tasks.py",
            "data_model/message.py",
            "data_model/simulation.py",
            "runner/simulation.py",
            "persistent_tasks/core/models.py",
            "persistent_tasks/core/observation.py",
            "persistent_tasks/adapters/qwen.py",
            "environment/environment.py",
            "environment/scenario.py",
            "environment/replay.py",
            "environment/execution.py",
            "orchestrator/scenario_stages.py",
            "evaluator/scenario_series.py",
            "evaluator/outcome.py",
            "orchestrator/orchestrator.py",
            "orchestrator/full_duplex_orchestrator.py",
            "evaluator/evaluator.py",
            "evaluator/evaluator_base.py",
            "evaluator/evaluator_env.py",
            "evaluator/scenario.py",
            "evaluator/evaluator_nl_assertions.py",
            "evaluator/evaluator_communicate.py",
            "user/user_simulator.py",
            "user/user_simulator_streaming.py",
            "user/dynamic_followup.py",
            "user/grounding.py",
            "voice/audio_native/qwen_agent/mimo_transcription.py",
            "voice/audio_native/qwen_agent/played_audio_transcription.py",
            "voice/audio_native/qwen_agent/qwen_transcription.py",
            "voice/qwen_asr.py",
            "data_model/asr.py",
            "user/asr_hotwords.py",
        ]
    )
    # Include extracted modules and provider contracts, not just their callers.
    for relative in (
        "persistent_tasks/core",
        "persistent_tasks/adapters",
    ):
        paths.extend((package / relative).glob("*.py"))
    paths.extend(
        package / relative
        for relative in (
            "persistent_tasks/availability.py",
            "voice/audio_native/qwen_agent/async_evidence.py",
            "utils/transcript/generated.py",
        )
    )
    digest = hashlib.sha256(GAIA2_VERSION.encode())
    from tau2.config import (
        DEFAULT_QWEN_AGENT_BACKEND_EVENT_QUEUE_SIZE,
        DEFAULT_SCENARIO_DRAIN_SECONDS,
        DEFAULT_SCENARIO_EVIDENCE_CHARS,
        DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS,
        DEFAULT_SCENARIO_POLL_SECONDS,
        DEFAULT_SCENARIO_STATE_MATCH_LIMIT,
        DEFAULT_USER_CANDIDATE_RETRIES,
    )

    digest.update(
        canonical_json(
            [
                DEFAULT_SCENARIO_POLL_SECONDS,
                DEFAULT_SCENARIO_DRAIN_SECONDS,
                DEFAULT_SCENARIO_NOTIFICATION_WAIT_SECONDS,
                DEFAULT_SCENARIO_EVIDENCE_CHARS,
                DEFAULT_SCENARIO_STATE_MATCH_LIMIT,
                DEFAULT_USER_CANDIDATE_RETRIES,
                DEFAULT_QWEN_AGENT_BACKEND_EVENT_QUEUE_SIZE,
            ]
        )
    )
    for path in sorted(set(paths)):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def canonical_json(value) -> bytes:
    """Serialize source artifacts reproducibly, independent of key order."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def read_snapshot(digest: str, root: Path = GAIA2_DATA_DIR) -> list[dict]:
    """Load and verify a snapshot, refusing path-shaped identifiers."""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid Gaia2 snapshot digest")
    raw = gzip.decompress((root / "snapshots" / f"{digest}.json.gz").read_bytes())
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Gaia2 snapshot content differs from its recorded hash")
    return json.loads(raw)

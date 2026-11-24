"""The frozen 2026-10-02 release export becomes the same 2.0 package as a matrix.

A small synthetic frozen export; no shipped task pack or model is needed.
"""

import argparse
import json
import shutil
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from tau2.data_model.benchmark import BenchmarkFrame, CellFrame
from tau2.data_model.simulation import RewardInfo, SimulationRun
from tau2.scripts.leaderboard.benchmark_package import (
    METADATA_FILE_NAME,
    SUBMISSION_FILE_NAME,
    PackageMetadata,
    SubmitterMetadata,
    load_package,
    package_name,
)
from tau2.scripts.leaderboard.benchmark_quality import QUALITY_FILE_NAME
from tau2.scripts.leaderboard.generate_schema import (
    METADATA_SCHEMA_PATH,
    QUALITY_SCHEMA_PATH,
    SCHEMA_PATH,
    generate_metadata_schema,
    generate_quality_schema,
    generate_schema,
)
from tau2.scripts.leaderboard.voice_v2_publication import (
    _validate_cell_frame,
    _validate_simulation_files,
)

SUBMITTER = {
    "model_name": "Offline Fixture 1.0",
    "model_organization": "Test Org",
    "submitting_organization": "Test",
    "submission_date": "2026-10-02",
    "contact_info": {"email": "review@example.org"},
}
NAME = "offline-fixture-1-0_test-org_2026-10-02"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
def forbid_paid_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Publication must use only saved offline inputs")

    monkeypatch.setattr("tau2.utils.llm_utils.generate", forbidden)
    monkeypatch.setattr("litellm.completion", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def publication_frame(monkeypatch):
    frame = BenchmarkFrame(
        benchmark_version="voice-v2",
        revision="offline-publication-v1",
        mode="voice",
        trials=[0],
        cells=[
            CellFrame(
                language=language,
                domain=domain,
                canonical_task_ids=[str(i) for i in range(4)],
                binding_revision="fixture-binding",
            )
            for language, domain in [("en", "airline"), ("en", "telecom")]
        ],
    )
    monkeypatch.setattr(
        "tau2.scripts.leaderboard.voice_v2_publication.load_frame",
        lambda *args, **kwargs: frame,
    )
    return frame


@pytest.fixture
def release_export(tmp_path, publication_frame):
    """Historical layout: trajectories/<lang>/<domain>/."""
    root = tmp_path / "release"
    source = root / "export"
    official = publication_frame
    for cell in official.cells:
        language, domain = cell.language, cell.domain
        directory = source / "trajectories" / language / domain
        index = []
        for position, task_id in enumerate(cell.canonical_task_ids):
            simulation_id = f"{domain}-{task_id}"
            reward = float(position % 2)
            simulation = SimulationRun(
                id=simulation_id,
                task_id=task_id,
                trial=0,
                timestamp="2026-10-02T00:00:00",
                start_time="2026-10-02T00:00:00",
                end_time="2026-10-02T00:00:01",
                duration=1,
                termination_reason="agent_stop",
                reward_info=RewardInfo(reward=reward),
            )
            _write_json(
                directory / "simulations" / f"{simulation_id}.json",
                simulation.model_dump(mode="json"),
            )
            index.append(
                {
                    "id": simulation_id,
                    "task_id": task_id,
                    "trial": 0,
                    "reward": reward,
                    "termination_reason": "agent_stop",
                }
            )
        _write_json(
            directory / "results.json",
            {
                "info": {
                    "benchmark": {
                        "benchmark_version": official.benchmark_version,
                        "language": language,
                        "domain": domain,
                        "selected_task_ids": cell.canonical_task_ids,
                        "canonical_task_ids": cell.canonical_task_ids,
                        "trials": [0],
                        "frame_revision": official.revision,
                        "binding_revision": cell.binding_revision,
                        "condition_fingerprint": "c" * 64,
                        "conditions": {
                            # Historical provider values the current parser
                            # rejects are read as recorded strings.
                            "audio_native_config": {
                                "provider": "openai_live",
                                "model": "offline-fixture",
                            }
                        },
                    },
                    "language": language,
                    "environment_info": {"domain_name": domain},
                    "agent_info": {"llm": "offline-fixture"},
                    "user_info": {"llm": "offline-caller"},
                    "num_trials": 1,
                    "language_tasks": {
                        task_id: {"canonical_task_id": task_id}
                        for task_id in cell.canonical_task_ids
                    },
                },
                "tasks": [{"id": task_id} for task_id in cell.canonical_task_ids],
                "simulation_index": index,
            },
        )
    metadata = root / "metadata.json"
    _write_json(metadata, SUBMITTER)
    return source, metadata


def _submit(*argv):
    from tau2.scripts.leaderboard.cli import add_submit_args

    parser = argparse.ArgumentParser()
    add_submit_args(parser)
    args = parser.parse_args([str(value) for value in argv])
    args.func(args)


@pytest.fixture
def package(release_export, tmp_path, forbid_paid_calls):
    source, metadata = release_export
    output = tmp_path / "out"
    _submit(
        "prepare",
        source,
        "--historical-voice-v2",
        "--metadata-file",
        metadata,
        "--output",
        output,
    )
    return output / NAME


def _validate(package, release_export):
    source, _ = release_export
    _submit(
        "validate",
        package,
        "--historical-voice-v2",
        "--results",
        source,
    )


def test_historical_export_is_a_canonical_package(package, release_export):
    loaded = load_package(package)
    assert loaded.metadata.evidence == "historical-voice-v2-2026-10-02"
    assert (loaded.metadata.agent_provider, loaded.metadata.agent_model) == (
        "openai_live",
        "offline-fixture",
    )
    summary = loaded.submission.summary
    assert summary.complete and len(loaded.submission.trials) == 8
    assert summary.score == pytest.approx(0.5)
    assert set(summary.by_language) == {"en"}
    assert not (package / "benchmark-diagnostics.json").exists()
    quality = loaded.quality
    assert (quality.benchmark_version, quality.frame_revision) == (
        "voice-v2",
        loaded.submission.frame.revision,
    )
    assert [(c.language, c.domain) for c in quality.cells] == [
        (c.language, c.domain) for c in loaded.submission.frame.cells
    ]
    en_airline_quality = quality.cells[0]
    assert len(en_airline_quality.evaluation_log["calls"]) == 4
    assert "metrics" not in en_airline_quality.model_dump()
    _validate(package, release_export)


def test_upload_layout_matches_the_uploaded_release_entry(package, release_export):
    """Review uploads use the S3 layout of the 2026-10-09 Gemini entry."""
    from tau2.scripts.leaderboard.review_submission import _package_uploads

    source, _ = release_export
    (source / "correction.json").write_text("{}")
    try:
        keys = {key for _, key in _package_uploads(load_package(package), source)}
    finally:
        (source / "correction.json").unlink()
    assert "trajectories/en/telecom/" in keys
    assert "source_correction.json" in keys
    assert not any(key.startswith(("judges/", "correction")) for key in keys)


def test_validate_rejects_changed_package_and_evidence(package, release_export):
    submission = json.loads((package / SUBMISSION_FILE_NAME).read_text())
    submission["summary"]["score"] = 0.9
    (package / SUBMISSION_FILE_NAME).write_text(json.dumps(submission))
    with pytest.raises(ValueError, match="summary does not match"):
        _validate(package, release_export)


def test_validate_recomputes_call_quality(package, release_export):
    path = package / QUALITY_FILE_NAME
    original = path.read_text()
    quality = json.loads(original)
    # Call provenance is still reproducible; no retired flag is calculated.
    quality["cells"][0]["evaluation_log"]["calls"][0]["simulation_id"] = "edited"
    path.write_text(json.dumps(quality))
    with pytest.raises(ValueError, match="Call quality differs"):
        _validate(package, release_export)
    path.unlink()
    with pytest.raises(ValueError, match=f"lacks {QUALITY_FILE_NAME}"):
        load_package(package)
    path.write_text(original)
    _validate(package, release_export)


def test_metadata_contract():
    submitter = SubmitterMetadata.model_validate(SUBMITTER)
    assert package_name(submitter) == NAME
    # One agent's packages share metadata; domain frames get distinct names.
    names = {
        version: package_name(submitter, version)
        for version in ("voice-v2", "text-v1", "voice-v2-banking", "voice-v2-entity")
    }
    assert names == {
        "voice-v2": NAME,
        "text-v1": NAME,
        "voice-v2-banking": f"{NAME}_banking",
        "voice-v2-entity": f"{NAME}_entity",
    }
    with pytest.raises(ValidationError):
        SubmitterMetadata.model_validate({**SUBMITTER, "contact_info": {}})
    with pytest.raises(ValidationError):
        SubmitterMetadata.model_validate({**SUBMITTER, "results": {}})
    with pytest.raises(ValidationError):
        PackageMetadata(
            **SUBMITTER,
            agent_provider="gemini",
            agent_model="m",
            trajectories="https://example.org/not-s3",
        )


def test_package_rejects_incomplete_or_misfiled_parts(package):
    loaded = load_package(package)
    text = loaded.model_copy(
        update={"metadata": loaded.metadata.model_copy(update={"agent_provider": None})}
    )
    with pytest.raises(ValueError, match="agent_provider"):
        text.check()
    (package / METADATA_FILE_NAME).unlink()
    with pytest.raises(ValueError, match="lacks benchmark-metadata.json"):
        load_package(package)


def test_frame_rejects_duplicate_task_trial(publication_frame) -> None:
    official = publication_frame
    cell = next(
        item
        for item in official.cells
        if (item.language, item.domain) == ("en", "airline")
    )
    task_ids = cell.canonical_task_ids
    simulations = [
        {
            "id": f"sim-{index}",
            "task_id": task_id,
            "trial": 0,
            "reward": 1.0,
            "termination_reason": "agent_stop",
        }
        for index, task_id in enumerate([*task_ids[:-1], task_ids[0]])
    ]
    data = {
        "info": {
            "benchmark": {
                "benchmark_version": official.benchmark_version,
                "frame_revision": official.revision,
                "binding_revision": cell.binding_revision,
                "canonical_task_ids": task_ids,
                "selected_task_ids": task_ids,
                "language": "en",
                "domain": "airline",
                "trials": [0],
            },
            "language": "en",
            "environment_info": {"domain_name": "airline"},
            "num_trials": 1,
            "language_tasks": {
                task_id: {"canonical_task_id": task_id} for task_id in task_ids
            },
        },
        "tasks": [{"id": task_id} for task_id in task_ids],
    }
    with pytest.raises(ValueError, match="missing or duplicate task/trial keys"):
        _validate_cell_frame(data, simulations, "en", "airline", official)


def test_index_must_agree_with_saved_simulation(release_export) -> None:
    source, _ = release_export
    results = json.loads((source / "trajectories/en/airline/results.json").read_text())
    index = [dict(entry) for entry in results["simulation_index"]]
    index[0]["reward"] = 1.0 - index[0]["reward"]
    with pytest.raises(ValueError, match="simulation/index reward mismatch"):
        _validate_simulation_files(source, index, "en", "airline")


def test_schemas_are_current():
    assert SCHEMA_PATH.read_text() == generate_schema()
    assert METADATA_SCHEMA_PATH.read_text() == generate_metadata_schema()
    assert QUALITY_SCHEMA_PATH.read_text() == generate_quality_schema()


def test_historical_infrastructure_errors_are_excluded_and_validate(
    release_export, tmp_path, forbid_paid_calls
):
    """Unscored infrastructure calls leave the denominator, not the package."""
    source, metadata = release_export
    copied = tmp_path / "export"
    shutil.copytree(source, copied)
    directory = copied / "trajectories" / "en" / "telecom"
    results = json.loads((directory / "results.json").read_text())
    # Positions 1 and 3 would have passed; their tasks drop out of the mean.
    for entry in (results["simulation_index"][1], results["simulation_index"][3]):
        entry.update(reward=None, termination_reason="infrastructure_error")
        path = directory / "simulations" / f"{entry['id']}.json"
        simulation = json.loads(path.read_text())
        simulation.update(reward_info=None, termination_reason="infrastructure_error")
        path.write_text(json.dumps(simulation))
    (directory / "results.json").write_text(json.dumps(results))
    output = tmp_path / "out"
    _submit(
        "prepare",
        copied,
        "--historical-voice-v2",
        "--metadata-file",
        metadata,
        "--output",
        output,
    )
    summary = load_package(output / NAME).submission.summary
    assert summary.complete and summary.error_trials == 2
    cell = next(c for c in summary.cells if (c.language, c.domain) == ("en", "telecom"))
    assert (cell.scored_trials, cell.error_trials) == (2, 2)
    assert cell.score == 0
    assert summary.score == 0.25
    _validate(output / NAME, (copied, metadata))

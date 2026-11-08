"""Outcome-preserving variants and versioned population validation."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tau2.task_authoring.gaia2_variants import build_variant, validate_variant


@pytest.fixture
def source_task():
    """Use a real native task so all reference and schema fields are covered."""
    path = Path("data/tau2/task_variants/gaia2/pilot-v1/tasks.json")
    task = json.loads(path.read_text())[0]
    source_path = Path("data/tau2/domains/gaia2/tasks.json")
    return next(
        source
        for source in json.loads(source_path.read_text())
        if source["id"] == task["scenario"]["source_id"]
    )


def test_first_stage_text_is_updated_everywhere(source_task):
    request = (
        source_task["scenario"]["stages"][0]["request"] + " Please handle that request."
    )
    variant = build_variant(source_task, "repetition", 0, request)
    assert variant["user_scenario"]["instructions"]["reason_for_call"] == request
    assert variant["evaluation_criteria"] == source_task["evaluation_criteria"]
    validate_variant(source_task, variant, 0)


def test_model_context_identifies_the_exact_target_request(source_task):
    from tau2.task_authoring.gaia2_variants import context_packet

    packet = context_packet(
        source_task,
        Path("data/tau2/domains/gaia2"),
        {"stage": 1, "subtype": "parameter_change"},
    )
    assert packet["target_stage_index"] == 1
    assert packet["target_stage_number"] == 2
    assert (
        packet["target_original_request"]
        == source_task["scenario"]["stages"][1]["request"]
    )


def test_changed_reference_and_unsynchronized_text_are_rejected(source_task):
    request = (
        source_task["scenario"]["stages"][2]["request"] + " Please handle that request."
    )
    variant = build_variant(source_task, "repetition", 2, request)
    changed = copy.deepcopy(variant)
    changed["evaluation_criteria"]["communicate_info"] = ["invented answer"]
    with pytest.raises(ValueError, match="reference, criteria"):
        validate_variant(source_task, changed, 2)
    changed = copy.deepcopy(variant)
    user = next(
        event
        for event in changed["scenario"]["events"]
        if event["id"] == "series_s2_user"
    )
    user["arguments"]["content"] = "Different request"
    with pytest.raises(ValueError, match="reference, criteria"):
        validate_variant(source_task, changed, 2)


def test_later_rewrite_preserves_initial_input_and_other_stages(source_task):
    source_copy = copy.deepcopy(source_task)
    request = (
        source_task["scenario"]["stages"][1]["request"] + " Please handle that request."
    )
    variant = build_variant(source_task, "mind_change", 1, request)
    assert variant["user_scenario"] == source_task["user_scenario"]
    assert variant["scenario"]["stages"][0] == source_task["scenario"]["stages"][0]
    assert variant["scenario"]["stages"][2] == source_task["scenario"]["stages"][2]
    assert source_task == source_copy
    validate_variant(source_task, variant, 1)


def test_export_rejects_incomplete_population(tmp_path):
    from tau2.task_authoring.gaia2_variants import export

    source = tmp_path / "source"
    source.mkdir()
    (source / "tasks.json").write_text("[]")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "source_root": str(source),
                "selection": [],
                "source_sha256": hashlib.sha256(b"[]").hexdigest(),
            }
        )
    )
    with pytest.raises(ValueError, match="200 variants"):
        export(dataset, tmp_path / "work")
    assert not (dataset / "tasks.json").exists()


def test_final_validation_binds_sealed_bytes_and_rejects_changed_content(
    source_task, tmp_path
):
    from tau2.domains.gaia2.utils import runtime_digest
    from tau2.task_authoring.gaia2_series.storage import digest
    from tau2.task_authoring.gaia2_variants import finalize_validation

    request_text = (
        source_task["scenario"]["stages"][2]["request"] + " Please handle that request."
    )
    draft = build_variant(source_task, "repetition", 2, request_text)
    sealed = copy.deepcopy(draft)
    sealed["scenario"]["runtime_sha256"] = runtime_digest()
    dataset = tmp_path / "dataset"
    destination = tmp_path / "published"
    dataset.mkdir()
    destination.mkdir()
    (dataset / "tasks.json").write_text(json.dumps([draft]))
    (dataset / "validation.json").write_text(
        json.dumps([{"id": draft["id"], "task_sha256": digest(draft)}])
    )
    (destination / "manifest.json").write_text(
        json.dumps({"verified": True, "runtime_sha256": runtime_digest()})
    )
    (destination / "tasks.json").write_text(json.dumps([sealed]))
    finalize_validation(dataset, destination)
    report = json.loads((destination / "variants.validation.json").read_text())
    assert report[0]["task_sha256"] == digest(sealed)
    sealed["scenario"]["stages"][2]["request"] = "Different business objective"
    (destination / "tasks.json").write_text(json.dumps([sealed]))
    with pytest.raises(ValueError, match="beyond reference seal fields"):
        finalize_validation(dataset, destination)


@pytest.mark.parametrize(
    "utterance",
    ["Use {{reference_event}} now.", "Email 0123456789abcdef0123456789abcdef."],
)
def test_requests_cannot_expose_internal_references(source_task, utterance):
    with pytest.raises(ValueError, match="internal identifier"):
        build_variant(source_task, "mind_change", 2, utterance)


def test_population_validation_accepts_versions_and_rejects_duplicate_ids():
    from types import SimpleNamespace

    from tau2.domains.gaia2.verify import population_errors

    counts = dict.fromkeys(
        ("search", "execution", "ambiguity", "adaptability", "time"), 160
    )
    tasks = [
        SimpleNamespace(
            id=f"{category}-{number}", scenario=SimpleNamespace(category=category)
        )
        for category, count in counts.items()
        for number in range(count)
    ]
    splits = {
        category: [item.id for item in tasks if item.scenario.category == category]
        for category in counts
    }
    splits["base"] = [item.id for item in tasks]
    manifest = {"tasks": 800, "categories": counts}
    assert not population_errors(tasks, splits, manifest)
    tasks[1].id = tasks[0].id
    assert population_errors(tasks, splits, manifest)
    tasks[1].id = "search-1"
    originals = list(tasks)
    for category, offset in (("mind_change", 0), ("repetition", 100)):
        counts[category] = 100
        variants = [
            SimpleNamespace(
                id=f"{original.id}__{category}_v1",
                scenario=SimpleNamespace(category=category, source_id=original.id),
            )
            for original in originals[offset : offset + 100]
        ]
        tasks.extend(variants)
        splits[category] = [item.id for item in variants]
    splits["base"] = [item.id for item in tasks]
    manifest.update(tasks=1000, population_version="gaia2-variants-v1")
    assert not population_errors(tasks, splits, manifest)
    splits["mind_change"][0] = "missing"
    assert population_errors(tasks, splits, manifest)
    manifest["population_version"] = "unknown-release"
    assert "Unknown Gaia2 population" in population_errors(tasks, splits, manifest)[0]

"""Focused regression checks for offline repair persistence and evidence."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from gaia2_repair_batches import (
    apply_patch,
    exclusive_output,
    export_results,
    graph_errors,
    record_result,
)
from gaia2_source_replay import reference_value, required_events
from repair_gaia2_references import read_json, sha, write_json


def task(task_id):
    return {
        "id": task_id,
        "user_scenario": {"instructions": "Keep the caller's request."},
        "scenario": {
            "start_time": 0,
            "source_id": task_id,
            "source_sha256": "0" * 64,
            "snapshot": "0" * 64,
            "category": "search",
            "events": [
                {
                    "id": "request",
                    "kind": "user",
                    "tool": "ask",
                    "arguments": {"content": "Keep the caller's request."},
                },
                {
                    "id": "answer",
                    "kind": "reference",
                    "tool": "answer",
                    "arguments": {"content": "old"},
                    "dependencies": ["request"],
                    "channel": "agent",
                },
            ],
            "stages": [
                {
                    "stage": 0,
                    "request": "Keep the caller's request.",
                    "event_ids": ["request", "answer"],
                    "criteria": {},
                }
            ],
        },
    }


def package(target):
    return {
        "task_id": target["id"],
        "target_type": "task",
        "current_target": target,
        "unresolved_findings": [{"index": 1}, {"index": 2}],
    }


def response(operations):
    return {
        "status": "complete",
        "operations": operations,
        "decisions": [
            {"index": 1, "status": "fix", "reason": "correct reference"},
            {"index": 2, "status": "unresolved", "reason": "missing source"},
        ],
    }


def test_cumulative_exports_and_restart_do_not_drop_previous_batch(tmp_path):
    a, b = task("a"), task("b")
    baseline = {"a": a, "b": b}
    op = {
        "op": "replace",
        "path": "/scenario/events/1/arguments/content",
        "value": "new",
    }
    first = record_result(package(a), response([op]), baseline, "first")
    write_json(tmp_path / "results/a.json", first)
    assert export_results(tmp_path, baseline, {})["processed"] == 1
    second = record_result(package(b), response([op]), baseline, "second")
    write_json(tmp_path / "results/b.json", second)
    report = export_results(tmp_path, baseline, {})
    assert report["processed"] == 2
    assert report["tasks_with_open_findings"] == 2
    before = (tmp_path / "repaired-tasks.json").read_bytes()
    export_results(tmp_path, baseline, {})  # restart/no-op
    assert (tmp_path / "repaired-tasks.json").read_bytes() == before
    assert all(
        row["scenario"]["events"][1]["arguments"]["content"] == "new"
        for row in read_json(tmp_path / "repaired-tasks.json")
    )
    assert sha(a) == first["before_sha256"]


def test_atomic_rejection_keeps_fix_findings_open():
    original = task("a")
    operations = [
        {
            "op": "replace",
            "path": "/scenario/events/1/arguments/content",
            "value": "new",
        },
        {"op": "replace", "path": "/missing/key", "value": 3},
    ]
    row = record_result(package(original), response(operations), {"a": original}, "key")
    assert row["target"] == original
    assert row["status"] == "failed"
    assert row["changed_paths"] == []
    assert row["unresolved_count"] == 2


@pytest.mark.parametrize("index", ["-1", "99", "x"])
def test_strict_patch_rejects_invalid_array_indexes(index):
    original = {"items": [1, 2]}
    with pytest.raises((ValueError, KeyError, IndexError)):
        apply_patch(original, [{"op": "add", "path": "/items/" + index, "value": 5}])
    assert original == {"items": [1, 2]}


def test_parent_replacement_cannot_change_user_requests():
    original = task("a")
    replacement = copy.deepcopy(original["scenario"])
    replacement["stages"][0]["request"] = "Invent a different task"
    row = record_result(
        package(original),
        response(
            [
                {"op": "replace", "path": "/scenario", "value": replacement},
            ]
        ),
        {"a": original},
        "key",
    )
    assert row["status"] == "failed"
    assert row["target"] == original


def test_event_closure_includes_complete_checkpoint_and_template_ancestors():
    target = task("a")
    target["scenario"]["events"].extend(
        [
            {"id": "extra", "arguments": {}, "dependencies": ["request"]},
            {"id": "later", "arguments": {"id": "{{answer}}"}, "dependencies": []},
        ]
    )
    target["scenario"]["stages"][0]["event_ids"].append("extra")
    needed = required_events(target, [{"reference": {"source": "state", "stage": 0}}])
    assert needed == {"request", "answer", "extra"}
    needed = required_events(
        target, [{"reference": {"source": "result", "event_id": "later"}}]
    )
    assert needed == {"later", "answer", "request"}


def test_checkpoint_resolution_uses_stage_and_dynamic_result_key():
    ref = {
        "source": "state",
        "stage": 1,
        "pointer": "/Calendar/events",
        "key_from_result": {"event_id": "create", "pointer": ""},
        "suffix_pointer": "/title",
    }
    assert (
        reference_value(
            ref,
            initial={},
            states={
                1: {"Calendar": {"events": {"generated-id": {"title": "actual"}}}},
            },
            results={"create": "generated-id"},
        )
        == "actual"
    )
    with pytest.raises(KeyError):
        reference_value(
            ref, initial={"Calendar": {}}, states={}, results={"create": "id"}
        )


def test_graph_detects_duplicate_ids_cycles_and_bad_stage_references():
    t = task("a")
    t["scenario"]["events"][0]["dependencies"] = ["answer"]
    assert any("cycle" in error for error in graph_errors(t))
    t["scenario"]["events"].append(t["scenario"]["events"][0])
    t["scenario"]["stages"][0]["event_ids"].append("missing")
    assert "duplicate event IDs" in graph_errors(t)
    assert "unknown stage event: missing" in graph_errors(t)


def test_export_refuses_baseline_drift(tmp_path):
    original = task("a")
    row = record_result(package(original), response([]), {"a": original}, "key")
    write_json(tmp_path / "results/a.json", row)
    altered = dict(original, description={"purpose": "changed baseline"})
    with pytest.raises(ValueError, match="Baseline changed"):
        export_results(tmp_path, {"a": altered}, {})


def test_output_lock_rejects_concurrent_writer_and_releases(tmp_path):
    with exclusive_output(tmp_path):
        with pytest.raises(RuntimeError, match="Another repair batch"):
            with exclusive_output(tmp_path):
                pass
    with exclusive_output(tmp_path):
        pass


def test_snapshot_state_and_result_sources_are_distinct():
    kwargs = dict(
        initial={"value": "original"},
        states={0: {"value": "after"}},
        results={"reply": {"content": "delivered"}},
    )
    assert (
        reference_value({"source": "snapshot", "pointer": "/value"}, **kwargs)
        == "original"
    )
    assert (
        reference_value({"source": "state", "stage": 0, "pointer": "/value"}, **kwargs)
        == "after"
    )
    assert (
        reference_value(
            {"source": "result", "event_id": "reply", "pointer": "/content"}, **kwargs
        )
        == "delivered"
    )


def test_large_source_values_stay_local_and_omissions_are_explicit():
    from repair_gaia2_unresolved import prompt_package_v2

    original = {
        "current_target": {"private_full_task": True},
        "source_evidence": {
            "findings": ["duplicated"],
            "stage_evidence": [
                {
                    "status": "verified",
                    "actual": "payload" * 3000,
                    "expected": "payload" * 3000,
                }
            ],
        },
    }
    model_view = prompt_package_v2(original)
    evidence = model_view["source_evidence"]["stage_evidence"][0]
    assert evidence["actual"]["omitted_large_value"] is True
    assert "expected" not in evidence
    assert "current_target" not in model_view
    assert (
        original["source_evidence"]["stage_evidence"][0]["actual"] == "payload" * 3000
    )


def test_sixteen_requests_really_overlap_and_batch_is_not_truncated(
    tmp_path, monkeypatch
):
    import json
    import threading

    import repair_gaia2_unresolved as repair
    from review_gaia2_references import ReviewConfig

    baseline = {}
    for number in range(16):
        original = task(f"parallel-{number:02d}")
        baseline[original["id"]] = original
        package = {
            "task_id": original["id"],
            "current_target": original,
            "target_type": "task",
            "unresolved_findings": [{"index": 1}],
        }
        write_json(tmp_path / "inputs" / (original["id"] + ".json"), package)
    rendezvous = threading.Barrier(16, timeout=20)
    calls = []

    class FakeModel:
        def __init__(self, config, cache):
            pass

        def complete(self, prompt, schema):
            calls.append(prompt)
            rendezvous.wait()  # fails if requests are actually serial or batch capped at 10
            return json.dumps(
                {
                    "decisions": [
                        {"finding_index": 1, "decision": "keep", "reason": "correct"}
                    ],
                    "operations": [],
                }
            )

    monkeypatch.setattr(repair, "DirectModel", FakeModel)
    monkeypatch.setattr(repair, "repair_prompt_v2", lambda package: package["task_id"])
    monkeypatch.setattr(repair, "load_v1", lambda root: (baseline, {}))
    kwargs = dict(
        v1_root=tmp_path / "baseline",
        review_dir=tmp_path,
        data_root=tmp_path,
        out=tmp_path,
        evidence_root=tmp_path,
        config=ReviewConfig(concurrency=16),
        limit=None,
        offline=False,
        batch_size=16,
    )
    summary = repair.run_v2(**kwargs)
    assert summary["selected_this_batch"] == 16
    assert summary["processed"] == 16
    assert len(calls) == 16
    assert read_json(tmp_path / "runtime-status.json")["max_active_requests"] == 16
    kwargs["config"] = ReviewConfig(concurrency=2)
    repair.run_v2(**kwargs)
    assert len(calls) == 16  # scheduling change does not invalidate model answers


def test_pending_batches_fill_holes_instead_of_advancing_a_cursor(tmp_path):
    from run_gaia2_source_repair_batches import pending_batches

    inputs = [tmp_path / "inputs" / f"task-{number:02d}.json" for number in range(34)]
    for index in list(range(10)) + list(range(16, 26)):
        write_json(
            tmp_path / "results" / inputs[index].name,
            {"evidence_review": "source_replay_resolved"},
        )
    plans = pending_batches(inputs, tmp_path, 16)
    flattened = [task_id for batch in plans for task_id in batch]
    expected = [
        path.stem
        for index, path in enumerate(inputs)
        if index not in list(range(10)) + list(range(16, 26))
    ]
    assert flattened == expected
    assert len(flattened) == len(set(flattened))

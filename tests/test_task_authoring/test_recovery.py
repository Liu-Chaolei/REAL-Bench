"""Tests for status routing in the resumable recovery coordinator."""

from pathlib import Path

import pytest

from tau2.task_authoring.gaia2_series import pipeline, recovery
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    ModelProfile,
    RequestPolicy,
)
from tau2.task_authoring.gaia2_series.storage import digest


def test_recovery_scope_routes_by_saved_stage_count(monkeypatch):
    from tau2.task_authoring.gaia2_series import recovery

    records = {
        "generation-error": {"status": "provider_error", "draft": {"stages": []}},
        "review-error": {"status": "review_error", "draft": {"stages": [{}, {}]}},
        "complete-provider-error": {
            "status": "provider_error",
            "draft": {"stages": [{}, {}]},
        },
        "partial": {"status": "partial", "draft": {"stages": [{}]}},
        "revision": {"status": "needs_revision", "draft": {"stages": []}},
        "reviewed": {"status": "reviewed", "draft": {"stages": [{}, {}]}},
    }
    monkeypatch.setattr(
        recovery,
        "source_tasks",
        lambda _: ({}, {task_id: object() for task_id in records}),
    )
    monkeypatch.setattr(recovery, "read_record", lambda _, task_id: records[task_id])

    assert recovery.recovery_scope(Path("dataset")) == {
        "generation": ["generation-error", "partial", "revision"],
        "review": ["complete-provider-error", "review-error"],
    }


def test_recovery_scope_is_read_only(monkeypatch):
    from tau2.task_authoring.gaia2_series import recovery

    calls = []
    monkeypatch.setattr(recovery, "source_tasks", lambda _: ({}, {"task": object()}))
    monkeypatch.setattr(
        recovery,
        "read_record",
        lambda _, task_id: calls.append(task_id) or {"status": "pending"},
    )

    assert recovery.recovery_scope(Path("dataset")) == {"generation": [], "review": []}
    assert calls == ["task"]


@pytest.mark.parametrize(
    "stage,prefix,expected",
    [(1, [], "repeated_candidate_failure"), (2, [], None), (1, [{}], None)],
)
def test_repeat_guard_is_bound_to_stage_and_prefix(stage, prefix, expected):
    record = {
        "candidate_history": [
            {
                "stage": stage,
                "prefix_sha256": digest(prefix),
                "valid": False,
                "error": "Invalid pointer /email_id",
                "candidate": {"request": str(i)},
            }
            for i in range(3)
        ]
    }
    assert (
        pipeline.generation_stop_reason(record, 3, repair=True, recovery_rounds=5)
        == expected
    )


def test_successful_candidate_breaks_the_repeated_error_streak():
    record = {
        "candidate_history": [
            {"stage": 1, "valid": False, "error": "Wrong pointer"} for _ in range(3)
        ]
    }
    record["candidate_history"].append({"stage": 1, "valid": True, "candidate": {}})
    assert pipeline.generation_stop_reason(record, 3, repair=True) is None


@pytest.fixture
def recovery_records(tmp_path, monkeypatch):
    import copy

    records = {
        "task": {
            "base_task_id": "task",
            "status": "needs_revision",
            "draft": {"stages": []},
        }
    }
    monkeypatch.setattr(
        recovery, "source_tasks", lambda _: ({}, dict.fromkeys(records))
    )
    monkeypatch.setattr(
        recovery, "read_record", lambda _, task_id: copy.deepcopy(records[task_id])
    )
    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )

    def run():
        return recovery.run_recovery(
            tmp_path,
            config,
            concurrency=1,
            repair_rounds=1,
            request_policy=RequestPolicy(),
        )

    return records, run


def test_exhausted_recovery_stays_unresolved_and_reports_no_progress(
    recovery_records, monkeypatch
):
    records, run = recovery_records
    calls = []

    def generate(*args, **kwargs):
        calls.append(True)
        records["task"]["recovery_stop"] = {
            "reason": "content_attempt_budget_exhausted",
            "stage": 1,
        }
        return {
            "results": [
                {
                    "id": "task",
                    "status": "needs_revision",
                    "skip_reason": "content_attempt_budget_exhausted",
                }
            ]
        }

    monkeypatch.setattr(pipeline, "generate_dataset", generate)
    result = run()
    assert len(calls) == 1
    assert result["status"] == "needs_attention"
    assert result["made_progress"] is False
    assert result["stop_reason"] == "no_progress"
    assert result["unresolved_tasks"] == ["task"]
    assert (
        result["blocked_tasks"]["task"]["reason"] == "content_attempt_budget_exhausted"
    )


def test_cached_rejection_reaches_repair_and_stalled_batch_stops(
    recovery_records, monkeypatch
):
    records, run = recovery_records
    generated, reviewed = [], []

    def generate(*args, **kwargs):
        generated.append(True)
        if len(generated) == 1:
            records["task"].update(status="verified", draft={"stages": [{}, {}]})
            return {"results": [{"id": "task", "status": "verified"}]}
        return {
            "results": [
                {
                    "id": "task",
                    "status": "needs_revision",
                    "skip_reason": "repeated_candidate_failure",
                }
            ]
        }

    def review(*args, **kwargs):
        reviewed.append(True)
        records["task"].update(
            status="needs_revision",
            review={
                "content_sha256": digest(records["task"]["draft"]),
                "accepted": False,
            },
        )
        return {"results": [{"id": "task", "status": "rejected"}]}

    monkeypatch.setattr(pipeline, "generate_dataset", generate)
    monkeypatch.setattr(pipeline, "verify_dataset", lambda *args: {"results": []})
    monkeypatch.setattr(pipeline, "review_dataset", review)
    result = run()
    assert len(generated) == 2
    assert len(reviewed) == 1
    assert result["status"] == "needs_attention"
    assert result["stop_reason"] == "repair_tasks_stalled"


def test_unchanged_content_stops_internal_cycles(recovery_records, monkeypatch):
    records, run = recovery_records
    records["task"].update(status="verified", draft={"stages": [{}, {}]})
    calls = []
    monkeypatch.setattr(
        pipeline,
        "review_dataset",
        lambda *args, **kwargs: {
            "results": [{"id": "task", "status": "needs_revision"}]
        },
    )
    monkeypatch.setattr(pipeline, "verify_dataset", lambda *args: {"results": []})

    def generate(*args, **kwargs):
        calls.append(True)
        return {"results": [{"id": "task", "status": "needs_revision"}]}

    monkeypatch.setattr(pipeline, "generate_dataset", generate)
    result = run()
    assert len(calls) == 1
    assert result["status"] == "needs_attention"
    assert result["stop_reason"] == "no_progress"


def test_verified_task_is_reviewed_before_completion(recovery_records, monkeypatch):
    records, run = recovery_records
    records["task"].update(status="verified", draft={"stages": [{}, {}]})

    def review(*args, **kwargs):
        records["task"]["status"] = "reviewed"
        return {"results": [{"id": "task", "status": "reviewed"}]}

    monkeypatch.setattr(pipeline, "review_dataset", review)
    monkeypatch.setattr(pipeline, "verify_dataset", lambda *args: {"results": []})
    result = run()
    assert result["status"] == "complete"
    assert result["made_progress"] is True
    assert result["progress"]["new_reviewed"] == 1


def test_unstarted_task_cannot_be_reported_complete(recovery_records):
    records, run = recovery_records
    records["task"]["status"] = "pending"
    result = run()
    assert result["status"] == "needs_attention"
    assert result["unresolved_tasks"] == ["task"]


@pytest.mark.parametrize(
    "status,stages,route",
    [
        ("generating", [], "generation"),
        ("verification_error", [], "generation"),
        ("verification_error", [{}, {}], "review"),
        ("verified", [{}, {}], "review"),
    ],
)
def test_interrupted_and_verified_tasks_are_not_lost(
    recovery_records, status, stages, route
):
    records, _ = recovery_records
    records["task"].update(status=status, draft={"stages": stages})
    assert recovery.recovery_scope(Path("dataset"))[route] == ["task"]


@pytest.mark.parametrize(
    "states,expected_code,expected_calls",
    [
        ([{"status": "needs_attention", "made_progress": False}], 2, 1),
        ([{"status": "failed"}], 2, 1),
        ([{"status": "complete", "made_progress": False}], 0, 1),
        (
            [
                {"status": "needs_attention", "made_progress": True},
                {"status": "complete", "made_progress": True},
            ],
            0,
            2,
        ),
    ],
)
def test_supervisor_stops_on_no_progress_and_continues_useful_work(
    tmp_path, states, expected_code, expected_calls
):
    import json
    import os
    import shutil
    import subprocess
    import sys

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "run_gaia2_recovery_loop.sh"
    shutil.copyfile(Path(__file__).parents[2] / "scripts" / script.name, script)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        "if sys.argv[1] == '-':\n"
        "    exec(compile(sys.stdin.read(), '<status>', 'exec'))\n"
        "else:\n"
        "    p = Path(sys.argv[sys.argv.index('--dataset') + 1])\n"
        "    calls = p / 'calls.json'\n"
        "    n = json.loads(calls.read_text()) if calls.exists() else 0\n"
        "    states = json.loads((p / 'states.json').read_text())\n"
        "    (p / 'recovery-run.json').write_text(json.dumps(states[n]))\n"
        "    calls.write_text(json.dumps(n + 1))\n"
    )
    python.chmod(0o755)
    sleep = python.parent / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "states.json").write_text(json.dumps(states))
    result = subprocess.run(
        ["bash", str(script), str(dataset), "unused.json"],
        env={**os.environ, "PATH": f"{python.parent}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected_code, result.stderr
    assert json.loads((dataset / "calls.json").read_text()) == expected_calls

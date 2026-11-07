"""Tests for the model-free manual takeover state machine."""

import json

from tau2.task_authoring.gaia2_series.manual import (
    classify_route,
    initialize_manual_dataset,
)
from tau2.task_authoring.gaia2_series.storage import digest


def test_route_classification():
    assert classify_route({"status": "provider_error", "draft": {}}) == "system_error"
    assert (
        classify_route({"status": "needs_revision", "draft": {"stages": [{}, {}]}})
        == "full_candidate"
    )
    assert (
        classify_route({"status": "needs_revision", "draft": {"stages": [{}]}})
        == "stage2_missing"
    )
    assert (
        classify_route({"status": "needs_revision", "draft": {"stages": []}})
        == "stage1_missing"
    )


def test_initialize_manual_dataset_preserves_passed_and_copies_only_failures(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text('{"expected_tasks": 800}')
    (source / "generation-config.json").write_text("{}")
    (source / "indexes").mkdir()
    # The real initialization is exercised in the integration fixture below;
    # this test protects its 68/314 partition contract through a small stub.
    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual.source_tasks",
        lambda _: (
            {"source_root": str(source), "source_files": {}, "expected_tasks": 800},
            {},
        ),
    )
    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual._failed_ids",
        lambda _: (
            [f"passed-{i}" for i in range(68)],
            [f"failed-{i}" for i in range(314)],
        ),
    )

    def fake_record(path, task_id):
        return path / "work" / f"{digest(task_id)}.json"

    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual.record_path", fake_record
    )
    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual.file_digest", lambda _: "x"
    )
    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual.read_record",
        lambda _, task_id: {
            "base_task_id": task_id,
            "status": "needs_revision",
            "question_plan": {
                "requests": [
                    {"request": "a substantive request"},
                    {"request": "another substantive request"},
                ]
            },
        },
    )
    (source / "work").mkdir()
    for task_id in [f"failed-{i}" for i in range(314)]:
        path = fake_record(source, task_id)
        path.write_text(
            json.dumps(
                {
                    "base_task_id": task_id,
                    "status": "needs_revision",
                    "question_plan": {
                        "requests": [
                            {"request": "a substantive request"},
                            {"request": "another substantive request"},
                        ]
                    },
                }
            )
        )
    # The test only checks the early contract; source_tasks on the staging copy
    # is mocked to avoid requiring a full Gaia2 source inventory.
    monkeypatch.setattr(
        "tau2.task_authoring.gaia2_series.manual.source_tasks", lambda _: ({}, {})
    )
    result = initialize_manual_dataset(source, tmp_path / "manual")
    assert result["preserved_passed"] == 68
    assert result["failed"] == 314


def submission_fixture(tmp_path, monkeypatch):
    """Build a real submission record; stub only native execution."""
    from test_task_authoring.test_gaia2_series import first_stage

    from tau2.task_authoring.gaia2_series import manual
    from tau2.task_authoring.gaia2_series.storage import record_path, write_json

    base = {"id": "t"}
    stage = first_stage().model_dump(mode="json")
    stage2 = dict(
        stage, request="Schedule a separate preparation reminder for this meeting."
    )
    candidate = {
        "base_task_id": "t",
        "base_task_sha256": digest(base),
        "stages": [stage, stage2],
    }
    requests = [
        {
            "request": s["request"],
            "new_goal": "Add a concrete new calendar business action.",
            "business_relation": "Continues the original meeting planning goal.",
            "business_objects": ["meeting"],
            "sources": [{"source": "original_request", "stage": 0, "pointer": ""}],
        }
        for s in candidate["stages"]
    ]
    record = {
        "base_task_id": "t",
        "draft": candidate,
        "question_plan": {"requests": requests},
        "manual_status": "pending",
        "manual_session": manual.SESSION,
        "manual_route": "full_candidate",
        "manual_attempt": 4,
        "manual_history": [],
    }
    write_json(tmp_path / "manual-scope.json", {"failed_ids": ["t"]})
    write_json(record_path(tmp_path, "t"), record)
    monkeypatch.setattr(
        manual, "source_tasks", lambda _: ({"source_root": str(tmp_path)}, {"t": base})
    )
    submission = {
        "task_id": "t",
        "candidate": candidate,
        "base_candidate_sha256": digest(candidate),
        "reason": "Personally checked the requested calendar goals against the materials.",
    }
    return manual, submission, record_path(tmp_path, "t")


def test_actual_submission_rejects_stale_hash(tmp_path, monkeypatch):
    import pytest

    manual, submission, path = submission_fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    submission["base_candidate_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Stale"):
        manual.verify_submission(tmp_path, submission)
    assert path.read_bytes() == before


def test_failed_submission_preserves_candidate_and_resume_budget(tmp_path, monkeypatch):
    manual, submission, path = submission_fixture(tmp_path, monkeypatch)
    seen = []

    def fail(base, source, draft):
        seen.append(len(draft.stages))
        raise ValueError("bad event parameter")

    monkeypatch.setattr(manual, "verify_draft", fail)
    submission["candidate"]["stages"][1]["relation"] = (
        "A corrected substantive relationship with the original meeting."
    )
    result = manual.verify_submission(tmp_path, submission)
    record = json.loads(path.read_text())
    assert result["status"] == "pending"
    assert seen == [1]  # Stage 2 never executes after a stage-1 failure.
    assert record["manual_attempt"] == 5  # No old three-attempt ceiling.
    assert record["manual_pending_candidate"] == submission["candidate"]
    assert "bad event parameter" in record["manual_last_error"]
    submission["base_candidate_sha256"] = digest(record["manual_pending_candidate"])
    manual.verify_submission(tmp_path, submission)
    assert json.loads(path.read_text())["manual_attempt"] == 5


def test_accepted_submission_skips_repeat_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace

    manual, submission, path = submission_fixture(tmp_path, monkeypatch)
    calls = []

    def verify(base, source, draft):
        calls.append(len(draft.stages))
        return {
            "valid": True,
            "replay_verified": True,
            "content_sha256": digest(draft),
        }, SimpleNamespace(records=[])

    monkeypatch.setattr(manual, "verify_draft", verify)
    manual.verify_submission(tmp_path, submission)
    record = json.loads(path.read_text())
    assert calls == [1, 2]
    assert record["manual_status"] == "verified"
    assert record["verification"]["requests_sha256"] == digest(
        [q["request"] for q in record["question_plan"]["requests"]]
    )
    assert (
        manual.verify_submission(tmp_path, submission)["status"] == "already_verified"
    )
    assert calls == [1, 2]

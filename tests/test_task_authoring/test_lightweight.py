"""Frozen scope, compact inputs and a shared lightweight worker for all tasks."""

import json
import threading
from types import SimpleNamespace

import pytest
from test_task_authoring.test_gaia2_series import (
    first_stage,
    make_base,
    second_stage,
)  # noqa: F401
from test_task_authoring.test_gaia2_series import (
    native_source as native_source,
)

from tau2.data_model.message import AssistantMessage, ToolCall
from tau2.task_authoring.gaia2_series import generation
from tau2.task_authoring.gaia2_series import lightweight as light
from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    ModelProfile,
)
from tau2.task_authoring.gaia2_series.solutions import (
    StageSolution,
    solution_from_stage,
)
from tau2.task_authoring.gaia2_series.storage import (
    digest,
    read_record,
    record_path,
    write_json,
)


def test_frozen_401_passed_and_399_scope_do_not_call_models(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    (source / "indexes").mkdir()
    ids = [f"task-{i:04d}" for i in range(800)]
    pending = ids[401:]
    write_json(
        source / "population.json",
        {
            "source_dataset": str(source),
            "records": {k: {"file_sha256": "unchanged"} for k in ids},
        },
    )
    write_json(
        source / "repair-queue.json",
        {
            "count": 399,
            "tasks": [
                {"base_task_id": k, "route": "semantic_precheck"} for k in pending
            ],
        },
    )
    write_json(source / "question-audit-report.json", {"recorded": 675})
    for k in ids[:401]:
        write_json(
            source / "question-audits" / f"{digest(k)}.json", {"verdict": "pass"}
        )
    monkeypatch.setattr(
        light, "source_tasks", lambda _: ({"expected_tasks": 800}, dict.fromkeys(ids))
    )
    monkeypatch.setattr(light, "file_digest", lambda _: "unchanged")
    monkeypatch.setattr(
        generation,
        "call_model",
        lambda *args, **kwargs: pytest.fail("Passed scope must not invoke models"),
    )
    frozen = light.prepare_scope(source, output)
    assert len(frozen["passed_ids"]) == 401 and len(frozen["repair_ids"]) == 399
    assert not set(frozen["passed_ids"]) & set(frozen["repair_ids"])
    assert set(frozen["passed_ids"]) | set(frozen["repair_ids"]) == set(ids)
    report = light.publish(output)
    assert report["passed"] == 401 and report["pending"] == 399
    write_json(source / "repair-queue.json", {"count": 800, "tasks": []})
    assert light.prepare_scope(source, output) == frozen


def test_question_lens_has_no_reference_answers_or_history():
    base = make_base()
    base.scenario.events[-1].arguments["content"] = "ORACLE_REPLY_CANARY"
    base.evaluation_criteria.communicate_info = ["GOLD_CANARY"]
    lens = light.question_lens(
        base,
        {"Contacts__get_contact": {}, "AgentUserInterface__send_message_to_user": {}},
    )
    text = json.dumps(lens)
    assert "CANARY" not in text and "AgentUserInterface" not in text
    assert "events" not in lens and "criteria" not in lens and "calls" not in lens


def test_semantic_precheck_preserves_requests_without_regeneration(monkeypatch):
    record = {
        "draft": {
            "stages": [
                {"request": "Keep the first saved request."},
                {"request": "Keep the second saved request."},
            ]
        }
    }
    monkeypatch.setattr(
        generation,
        "call_model",
        lambda *args, **kwargs: pytest.fail(
            "Preserved question must not be regenerated"
        ),
    )
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/test"), reviewer=ModelProfile(model="mimo/test")
    )
    requests, audit = light._questions(
        make_base(), {}, {"route": "semantic_precheck"}, record, config, []
    )
    assert requests == [q["request"] for q in record["draft"]["stages"]]
    assert audit["verdict"] == "pass"


def test_short_feedback_excludes_history_and_bounds_large_values():
    from tau2.task_authoring.gaia2_series.failures import CandidateValidationError

    error = CandidateValidationError(
        "invalid pointer",
        phase="evidence",
        actual={"sample": "x" * 10000},
        raw_reply="HISTORY_CANARY",
        current_candidate={"long": "z" * 10000},
    )
    result = light.short_feedback(error, {"request": "current"})
    assert len(json.dumps(result)) < 2000 and "CANARY" not in json.dumps(result)
    assert "current_candidate" not in result


def _working(native, tmp_path):
    task, _, source = native
    existing = read_record(source, task.id)
    write_json(
        record_path(source, task.id),
        {
            **existing,
            "base_task_id": task.id,
            "calls": [{"content": "OLD_CALL_CANARY"}],
            "question_plan": {
                "requests": [
                    {"request": first_stage().request},
                    {
                        "request": "Move that reminder to 2 PM on the same day and keep its duration and title."
                    },
                ]
            },
        },
    )
    output = tmp_path / "light"
    output.mkdir()
    (output / "indexes").symlink_to(source / "indexes", target_is_directory=True)
    write_json(
        output / "manifest.json", json.loads((source / "manifest.json").read_text())
    )
    write_json(
        output / "scope.json",
        {
            "source_dataset": str(source),
            "repair_ids": [task.id],
            "passed_ids": [],
            "expected_tasks": 1,
        },
    )
    write_json(
        output / "repair-scope-399.json",
        {"tasks": [{"base_task_id": task.id, "route": "semantic_precheck"}]},
    )
    write_json(output / "task-status.json", {task.id: {"status": "pending"}})
    return output


@pytest.mark.parametrize("stage1_fails", [False, True])
def test_shared_worker_clean_inputs_stage_gating_persisted_limits(
    native_source,  # noqa: F811
    tmp_path,
    monkeypatch,
    stage1_fails,  # noqa: F811
):
    output = _working(native_source, tmp_path)
    task, _, source = native_source
    original = record_path(source, task.id).read_bytes()
    packets = []

    def call(config, profile, rules, packet, response_model, reader, calls, **kwargs):
        packets.append(packet)
        policy = generation.request_policy_override.get()
        assert policy.per_key_concurrency == 6
        assert policy.api_key_pool == ["MIMO_API_KEY1", "MIMO_API_KEY3"]
        if response_model is StageSolution:
            if stage1_fails:
                raise ValueError("Bad structure, identical each time")
            if packet["stage_number"] == 1:
                return solution_from_stage(first_stage())
            event_id = next(
                iter(reader.reader.execution.states[1]["Calendar"]["events"])
            )
            return solution_from_stage(second_stage(event_id))
        if response_model is light.LightReview:
            return light.LightReview(
                verdict="pass", reasons=["Business goals verified."]
            )
        pytest.fail("Unexpected model type")

    monkeypatch.setattr(generation, "call_model", call)
    config = AuthoringConfig(
        author=ModelProfile(model="mimo/test"), reviewer=ModelProfile(model="mimo/test")
    )
    result = light.repair_one(output, task.id, config, per_key_concurrency=6)
    assert "OLD_CALL_CANARY" not in json.dumps(packets)
    assert record_path(source, task.id).read_bytes() == original
    if stage1_fails:
        assert result["status"] == "blocked" and result["attempts"]["1"] == 2
        assert not any(p.get("stage_number") == 2 for p in packets)
    else:
        assert result["status"] == "passed", result
        assert light.publish(output)["passed"] == 1
    count = len(packets)
    assert light.repair_one(output, task.id, config)["status"] == result["status"]
    assert len(packets) == count


@pytest.mark.parametrize("already_blocked", [0, 4])
@pytest.mark.parametrize("workers", [2, 12])
def test_two_empty_batches_stop_without_scheduling_passed_tasks(
    tmp_path, monkeypatch, already_blocked, workers
):
    ids = [f"repair{i:02d}" for i in range(25)]
    write_json(
        tmp_path / "scope.json",
        {"repair_ids": ids, "passed_ids": ["passed"], "expected_tasks": 26},
    )
    write_json(
        tmp_path / "task-status.json",
        {
            **{
                k: {"status": "blocked" if i < already_blocked else "pending"}
                for i, k in enumerate(ids)
            },
            "passed": {"status": "passed"},
        },
    )
    seen = []

    def worker(dataset, task_id, config, **kwargs):
        seen.append(task_id)
        light._status(tmp_path, task_id, "blocked")
        return {"task_id": task_id, "status": "blocked"}

    monkeypatch.setattr(light, "repair_one", worker)
    report = light.run_queue(tmp_path, None, concurrency=workers)
    assert report["stop_reason"] == "two_batches_without_progress"
    assert 20 - already_blocked <= len(seen) <= 25 - already_blocked
    assert "passed" not in seen
    assert not set(ids[:already_blocked]) & set(seen)
    first = json.loads((tmp_path / "batches/repair-001.json").read_text())
    assert {r["task_id"] for r in first["results"]} == set(ids[:10])
    assert report["passed"] == 1
    assert report["pending"] == 25 - already_blocked - len(seen)
    seen_before = list(seen)
    assert light.run_queue(tmp_path, None)["stop_reason"] == report["stop_reason"]
    assert seen == seen_before


@pytest.mark.parametrize("workers", [12, 24, 64])
@pytest.mark.parametrize("previous_empty_batch", [False, True])
def test_overlapping_batches_fill_workers_and_preserve_ten_task_reports(
    tmp_path, monkeypatch, workers, previous_empty_batch
):
    already_blocked = 10 if previous_empty_batch else 0
    task_count = max(30, ((workers + 9) // 10) * 10) + already_blocked
    ids = [f"repair{i:02d}" for i in range(task_count)]
    write_json(
        tmp_path / "scope.json",
        {
            "repair_ids": ids,
            "passed_ids": ["passed"],
            "expected_tasks": task_count + 1,
        },
    )
    write_json(
        tmp_path / "task-status.json",
        {
            **{
                k: {"status": "blocked" if i < already_blocked else "pending"}
                for i, k in enumerate(ids)
            },
            "passed": {"status": "passed"},
        },
    )
    if previous_empty_batch:
        write_json(
            tmp_path / "batches/repair-001.json",
            {
                "batch": 1,
                "new_passed": 0,
                "results": [{"task_id": k, "status": "blocked"} for k in ids[:10]],
            },
        )
    barrier, lock = threading.Barrier(workers), threading.Lock()
    active = peak = started = 0

    def worker(dataset, task_id, config, *, per_key_concurrency, key_limits):
        nonlocal active, peak, started
        assert per_key_concurrency == workers // 2
        assert key_limits == {}
        with lock:
            active += 1
            peak = max(peak, active)
            started += 1
            first_wave = started <= workers
        try:
            if first_wave:
                barrier.wait(timeout=10)
            light._status(tmp_path, task_id, "passed")
            return {"task_id": task_id, "status": "passed"}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(light, "repair_one", worker)
    result = light.run_queue(
        tmp_path, None, concurrency=workers, per_key_concurrency=workers // 2
    )
    assert peak == workers
    assert result["passed"] == task_count - already_blocked + 1
    batches = sorted((tmp_path / "batches").glob("repair-*.json"))
    assert len(batches) == task_count // 10
    assert all(len(json.loads(p.read_text())["results"]) == 10 for p in batches)
    before = started
    light.run_queue(tmp_path, None)
    assert started == before
    saved = json.loads((tmp_path / "runtime-settings.json").read_text())
    assert saved["concurrency"] == workers
    assert saved["per_key_concurrency"] == workers // 2


def test_four_reading_rounds_then_one_format_correction(monkeypatch):
    from tau2.environment.tool import Tool
    from tau2.utils import llm_utils

    def probe() -> str:
        """Read fixture."""
        return "data"

    reader = SimpleNamespace(tools=lambda: [Tool(func=probe)])
    requests = []

    def generate(**kwargs):
        requests.append([t.name for t in kwargs["tools"]])
        if len(requests) <= 4:
            return AssistantMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(id=str(len(requests)), name="probe", arguments={})
                ],
            )
        if len(requests) == 5:
            return AssistantMessage(role="assistant", content="not submitted")
        return AssistantMessage(
            role="assistant",
            tool_calls=[
                ToolCall(id="ok", name="submit", arguments={"source": "state"})
            ],
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    from tau2.task_authoring.gaia2_series.schema import ValueRef

    config = AuthoringConfig(
        author=ModelProfile(model="fake/a"), reviewer=ModelProfile(model="fake/b")
    )
    token = generation.request_policy_override.set(None)
    try:
        generation.call_model(
            config,
            config.author,
            "rules",
            {},
            ValueRef,
            reader,
            [],
            submission_name="submit",
            retrieval_rounds=4,
        )
    finally:
        generation.request_policy_override.reset(token)
    assert len(requests) == 6 and requests[-2:] == [["submit"], ["submit"]]

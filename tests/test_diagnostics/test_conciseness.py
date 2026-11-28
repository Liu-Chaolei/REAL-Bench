"""Feedback semantics, evidence isolation, calibration and resumable integration."""

import argparse
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tau2.data_model.diagnostics import DiagnosticEvidence, OperationEvidence, UserInput
from tau2.diagnostics.conciseness import INVENTORY_VERSION, VERSION
from tau2.diagnostics.conciseness.calibration import fit_calibration
from tau2.diagnostics.conciseness.evaluator import (
    delivered_history,
    evaluate_feedback,
    validate_alignment,
    validate_inventory,
)
from tau2.diagnostics.conciseness.evidence import (
    build_windows,
    generation_input,
    preflight,
)
from tau2.diagnostics.conciseness.models import Alignment, FeedbackConfig, LengthBudget
from tau2.diagnostics.conciseness.scoring import (
    aggregate_reports,
    response_length,
    score_window,
    trial_summary,
)
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.intent.io import canonical, digest
from tau2.diagnostics.model import RequestBudget


@pytest.fixture
def evidence():
    return DiagnosticEvidence(
        simulation_id="sim",
        task_id="task",
        domain="gaia2",
        user_inputs=[
            UserInput(
                id="u0", text="Cancel and refund my order", started=0, ended=1, stage=0
            ),
            UserInput(
                id="a0",
                role="assistant_context",
                text="TARGET_SENTINEL: cancelled, refund failed",
                started=3,
                ended=4,
                stage=0,
            ),
        ],
        stages={
            "stages": [
                {"stage": 0, "started": {"time": 1000}, "completed": {"time": 1001}}
            ]
        },
        operations=[
            OperationEvidence(
                call_id="op0",
                name="Orders__cancel",
                app="Orders",
                kind="WRITE",
                raw_arguments={"id": "order"},
                arguments={"id": "order"},
                result={"cancelled": True, "refund": "failed"},
                started=1001,
                ended=1002,
            )
        ],
    )


def inventory(required=2, optional=0):
    return {
        "status": "complete",
        "reason": "Grounded feedback",
        "outcome": "partial_success",
        "nuggets": [
            {
                "id": f"n{i}",
                "proposition": f"Required fact {i}",
                "necessity": "required" if i < required else "optional",
                "kind": "result",
                "reason": "Needed to understand outcome",
                "evidence_refs": ["user:u0", "operation:op0"],
                "equivalence_group": f"g{i}",
            }
            for i in range(required + optional)
        ],
    }


def alignment(inv, status="matched"):
    return {
        "matches": [
            {
                "nugget_id": n["id"],
                "status": status,
                "reason": "Evidence match",
                "quotes": [{"utterance_id": "a0", "text": "cancelled, refund failed"}],
            }
            for n in inv["nuggets"]
        ],
        "findings": [],
    }


def budget():
    return LengthBudget(base=10, per_nugget=15, optional_cap=0.25)


@pytest.mark.parametrize(
    "matched,length,recall,efficiency,score",
    [
        (4, 60, 1, 1, 100),
        (4, 140, 1, 0.5, 90.9090909),
        (1, 20, 0.25, 1, 27.027027),
        (0, 10, 0, 0, 0),
    ],
)
def test_trec_formula(matched, length, recall, efficiency, score):
    inv = inventory(4)
    aligned = alignment(inv, "missing")
    for row in aligned["matches"][:matched]:
        row["status"] = "matched"
    result = score_window(inv, aligned, [{"text": "x" * length}], budget())
    assert result["required_coverage"] == recall
    assert result["length_efficiency"] == efficiency
    assert result["feedback_score"] == pytest.approx(score)
    assert not any(k.startswith("raw_feedback_score") for k in result)
    assert "fidelity_status" not in result


def test_uncalibrated_never_publishes_length_score():
    inv = inventory()
    score = score_window(inv, alignment(inv), [{"text": "Cancelled"}])
    assert score["required_coverage"] == 1
    assert score["length_efficiency"] is score["feedback_score"] is None
    assert score["reason"] == "uncalibrated_language"


def test_unknown_keeps_denominator_and_score_interval():
    inv = inventory()
    aligned = alignment(inv)
    aligned["matches"][1]["status"] = "uncertain"
    score = score_window(inv, aligned, [{"text": "x" * 80}], budget())
    assert score["required_total"] == 2
    assert score["required_coverage"] is None
    assert score["required_coverage_lower"] == 0.5
    assert score["required_coverage_upper"] == 1
    assert score["feedback_score"] is None
    assert score["feedback_score_lower"] < score["feedback_score_upper"]


def test_optional_cap_and_equivalent_nuggets_do_not_inflate_budget():
    inv = inventory(2, 20)
    inv["nuggets"].append({**inv["nuggets"][0], "id": "duplicate"})
    score = score_window(inv, alignment(inv), [{"text": "x" * 100}], budget())
    assert score["required_total"] == 2
    assert score["length_budget"] == 47.5


def test_splitting_replies_does_not_add_budget():
    inv = inventory()
    combined = score_window(inv, alignment(inv), [{"text": "x" * 80}], budget())
    split = score_window(inv, alignment(inv), [{"text": "x" * 20}] * 4, budget())
    assert combined == split
    assert response_length([{"text": "e\u0301 中\n A"}]) == 3


def test_wrong_nugget_reduces_coverage_without_zeroing_other_credit():
    inv = inventory()
    aligned = alignment(inv)
    assert (
        score_window(inv, aligned, [{"text": "x"}], budget())["feedback_score"] == 100
    )
    aligned["matches"][0]["status"] = "contradicted"
    score = score_window(inv, aligned, [{"text": "x"}], budget())
    assert score["required_coverage"] == 0.5
    assert score["feedback_score"] == pytest.approx(100 * 5 / 9.5)
    assert score["feedback_score_lower"] == score["feedback_score_upper"]
    assert inv["outcome"] == "partial_success"


def test_blind_projection_excludes_target_and_future_request(evidence):
    evidence.user_inputs.append(
        UserInput(id="u1", text="FUTURE_SECRET", started=10, ended=11, stage=1)
    )
    evidence.stages["stages"].append(
        {"stage": 1, "started": {"time": 1010}, "completed": {"time": 1011}}
    )
    evidence.operations.append(
        evidence.operations[0].model_copy(
            update={
                "call_id": "future",
                "started": 1012,
                "ended": 1013,
                "result": "FUTURE_OPERATION",
            }
        )
    )
    first = build_windows(evidence, FeedbackConfig())[0]
    blind = canonical(generation_input(first, []))
    assert "TARGET_SENTINEL" not in blind
    assert "FUTURE_SECRET" not in blind
    assert "FUTURE_OPERATION" not in blind
    assert "op0" in blind
    assert len(first["replies"]) == 1


def test_slow_operation_does_not_leak_future_completion(evidence):
    evidence.user_inputs.append(
        UserInput(id="u1", text="Next request", started=10, ended=11, stage=1)
    )
    evidence.stages["stages"].append(
        {"stage": 1, "started": {"time": 1010}, "completed": {"time": 1011}}
    )
    evidence.operations[0].ended = 1015
    evidence.operations[0].result = "LATER_RESULT"
    first = build_windows(evidence, FeedbackConfig())[0]
    assert "LATER_RESULT" not in canonical(first["sources"])
    assert (
        first["sources"]["operation:op0"]["completion"]
        == "not_observed_before_window_end"
    )


def test_partial_delivery_is_not_counted_as_silence(evidence):
    evidence.user_inputs[1].complete = False
    report = preflight(evidence, FeedbackConfig())
    assert report["ready_windows"] == 0
    assert report["max_new_model_requests"] == 0
    assert report["windows"][0]["reason"] == "unverified_assistant_delivery"


def test_no_stage_does_not_guess_multiple_business_boundaries(evidence):
    evidence.stages = {}
    evidence.user_inputs.append(
        UserInput(id="u1", text="Another request", started=6, ended=7)
    )
    assert (
        build_windows(evidence, FeedbackConfig())[0]["reason"]
        == "unsupported_request_boundaries"
    )


def test_inventory_must_be_grounded_and_complete(evidence):
    window = build_windows(evidence, FeedbackConfig())[0]
    inv = inventory()
    inv["nuggets"][0]["evidence_refs"] = ["invented"]
    with pytest.raises(ValueError, match="Unknown nugget"):
        validate_inventory(canonical(inv), window["sources"])
    with pytest.raises(ValueError, match="required"):
        validate_inventory(canonical(inventory(0, 1)), window["sources"])


def test_invalid_quotes_are_unknown_not_guessed_credit(evidence):
    inv = inventory()
    aligned = alignment(inv)
    aligned["matches"][0]["quotes"][0]["text"] = "not spoken"
    value, errors = validate_alignment(
        canonical(aligned), inv, build_windows(evidence, FeedbackConfig())[0]
    )
    assert value["matches"][0]["status"] == "uncertain"
    assert value["matches"][1]["status"] == "matched"
    assert "fidelity_status" not in value
    assert errors


def test_alignment_cannot_drop_or_duplicate_nuggets(evidence):
    inv = inventory()
    aligned = alignment(inv)
    aligned["matches"].pop()
    with pytest.raises(ValueError, match="every frozen"):
        validate_alignment(
            canonical(aligned), inv, build_windows(evidence, FeedbackConfig())[0]
        )


def test_alignment_schema_only_requests_coverage_and_expression():
    inv = inventory()
    aligned = alignment(inv)
    assert Alignment.model_validate(aligned).model_dump() == aligned
    assert set(Alignment.model_json_schema()["properties"]) == {"matches", "findings"}
    aligned["fidelity_status"] = "material_error"
    with pytest.raises(ValidationError):
        Alignment.model_validate(aligned)
    aligned.pop("fidelity_status")
    aligned["findings"] = [
        {
            "kind": "material_error",
            "reason": "Old verdict",
            "quotes": [{"utterance_id": "a0", "text": "cancelled"}],
            "evidence_refs": ["operation:op0"],
        }
    ]
    with pytest.raises(ValidationError):
        Alignment.model_validate(aligned)


def test_two_stages_freeze_inventory_across_target_rewrites(
    evidence, tmp_path, monkeypatch
):
    calls = []

    class Model:
        def __init__(self, config, role, directory, budget):
            self.role, self.budget = role, budget

        def complete_sync(self, prompt, schema, *, validate):
            self.budget.take()
            calls.append((self.role, prompt))
            raw = canonical(
                inventory()
                if self.role == "feedback_nuggets"
                else alignment(inventory())
            )
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    config = MetricsConfig(measures=["conciseness"])
    directory = tmp_path / "diagnostics/evidence"
    first = evaluate_feedback(
        evidence, config, directory, tmp_path / "eval1", RequestBudget(8), "en"
    )
    assert first["trial"]["required_coverage"] == 1
    assert first["trial"]["feedback_score"] is None
    assert len(calls) == 2
    assert "TARGET_SENTINEL" not in calls[0][1]
    evidence.user_inputs[1].text += " Extra padding"
    second = evaluate_feedback(
        evidence, config, directory, tmp_path / "eval2", RequestBudget(8), "en"
    )
    assert len(calls) == 3  # Frozen obligations reused; second phase re-evaluated.
    assert (
        first["windows"][0]["nuggets_sha256"] == second["windows"][0]["nuggets_sha256"]
    )
    assert (
        first["windows"][0]["response_length"] < second["windows"][0]["response_length"]
    )


def test_alignment_failure_resumes_without_redrawing_standard(
    evidence, tmp_path, monkeypatch
):
    calls = []

    class Model:
        def __init__(self, config, role, directory, budget):
            self.role = role

        def complete_sync(self, prompt, schema, *, validate):
            calls.append(self.role)
            if len(calls) == 2:
                raise TimeoutError()
            raw = canonical(
                inventory()
                if self.role == "feedback_nuggets"
                else alignment(inventory())
            )
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    for name in ("failed", "resumed"):
        result = evaluate_feedback(
            evidence,
            MetricsConfig(),
            tmp_path / "evidence",
            tmp_path / name,
            RequestBudget(8),
        )
    assert calls == ["feedback_nuggets", "feedback_alignment", "feedback_alignment"]
    assert result["trial"]["required_coverage"] == 1


def test_verified_silence_is_zero_coverage(evidence, tmp_path, monkeypatch):
    evidence.user_inputs.pop()

    class Model:
        def __init__(self, *args):
            pass

        def complete_sync(self, prompt, schema, *, validate):
            raw = canonical(inventory())
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    result = evaluate_feedback(
        evidence,
        MetricsConfig(),
        tmp_path / "evidence",
        tmp_path / "out",
        RequestBudget(8),
    )
    assert result["trial"]["required_coverage"] == 0
    assert result["windows"][0]["response_length"] == 0


def test_partial_trial_not_averaged_over_successful_windows():
    inv = inventory()
    measured = score_window(inv, alignment(inv), [{"text": "x"}], budget())
    result = trial_summary([measured, {"status": "grader_error"}])
    assert result["feedback_score"] is None
    assert result["window_coverage"] == 0.5


def test_task_equal_aggregation_and_failure_cohort():
    def row(task, score, outcome):
        measure = {
            "language": "en",
            "domain": "gaia2",
            "status": "measured",
            "trial": {"feedback_score": score},
            "windows": [{"outcome": outcome, "feedback_score": score}],
        }
        return {
            "task_id": task,
            "reward": 0,
            "summary": {"measures": {"conciseness": measure}},
        }

    result = aggregate_reports(
        [
            row("a", 100, "success"),
            row("a", 100, "success"),
            row("b", 0, "failed_confirmed"),
            {},
        ]
    )
    assert result["feedback_score"] == 50
    assert result["score_coverage"] == 0.75
    assert result["outcomes"]["failed_confirmed"]["windows"] == 1
    assert result["task_success_rate"] == 0


def reviews():
    def case(identity, split, n, text):
        review = {
            "approved": True,
            "inventory_correct": True,
            "natural_and_complete": True,
            "required_count": n,
            "optional_count": 0,
            "concise_response": text,
        }
        return {
            "id": identity,
            "task_id": identity,
            "language": "en",
            "split": split,
            "reviews": [{**review, "reviewer": "Alice"}, {**review, "reviewer": "Bob"}],
        }

    return {
        "version": "tau-feedback-review/1",
        "cases": [
            case("a", "development", 1, "Cancelled"),
            case("b", "development", 2, "Cancelled. Refund pending."),
            case("c", "validation", 1, "Cancelled"),
        ],
    }


def test_calibration_requires_humans_and_does_not_fit_holdout(tmp_path):
    path = tmp_path / "reviews.json"
    value = reviews()
    path.write_text(json.dumps(value))
    first = fit_calibration(path, "test-release", "Reviewed development references")
    value["cases"][-1]["reviews"][0]["concise_response"] *= 100
    path.write_text(json.dumps(value))
    second = fit_calibration(path, "test-release", "Reviewed development references")
    assert first["calibration"]["languages"] == second["calibration"]["languages"]
    value["cases"][0]["reviews"] = []
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="two approving"):
        fit_calibration(path, "bad", "test")


def test_calibration_rejects_task_leakage(tmp_path):
    value = reviews()
    value["cases"][-1]["task_id"] = "a"
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="disjoint tasks"):
        fit_calibration(path, "bad", "test")


def test_cli_opt_in_and_default_compatibility():
    from tau2.diagnostics.cli import add_metrics_args

    parser = argparse.ArgumentParser()
    add_metrics_args(parser)
    args = parser.parse_args(
        [
            "run",
            "saved-run",
            "--config",
            "config.yaml",
            "--only",
            "conciseness",
            "--dry-run",
        ]
    )
    assert args.only == "conciseness"
    assert args.dry_run
    assert "conciseness" not in MetricsConfig().measures
    assert MetricsConfig(measures=["conciseness"]).conciseness.calibration is None
    with pytest.raises(ValidationError):
        LengthBudget(base=0, per_nugget=float("nan"), optional_cap=0.25)


def test_suite_integration_keeps_original_reward(evidence, tmp_path, monkeypatch):
    from tau2.diagnostics.suite import evaluate_evidence, summarize

    expected = {
        "version": VERSION,
        "status": "measured",
        "windows": [],
        "trial": {"feedback_score": None},
    }
    monkeypatch.setattr(
        "tau2.diagnostics.conciseness.evaluator.evaluate_feedback",
        lambda *args: copy.deepcopy(expected),
    )
    task = SimpleNamespace(model_dump=lambda **kw: {"id": "task"})
    result = evaluate_evidence(
        task, evidence, tmp_path / "evidence", MetricsConfig(measures=["conciseness"])
    )
    assert (
        result["summary"]["measures"]["conciseness"]["trial"]["feedback_score"] is None
    )
    report = summarize([{**result, "task_id": "task", "reward": 0}])
    assert report["metrics"]["conciseness"]["feedback_score"] is None
    assert report["evaluation_log"]["records"][0]["reward"] == 0


def test_malformed_model_response_is_audited_without_retry(tmp_path, monkeypatch):
    from tau2.diagnostics.model import TauJsonModel
    from tau2.utils import llm_utils

    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="not JSON", usage={}, cost=0)

    monkeypatch.setattr(llm_utils, "generate", generate)
    config = MetricsConfig()
    model = TauJsonModel(config, "feedback_nuggets", tmp_path, RequestBudget(2))
    with pytest.raises(ValueError):
        model.complete_sync("evidence", {})
    assert len(calls) == 1
    assert calls[0]["num_retries"] == 0
    attempt = json.loads(next((tmp_path / "attempts").glob("*.json")).read_text())
    assert attempt["raw_response"] == "not JSON"
    assert not list(tmp_path.glob("*.json"))


def test_contradiction_is_only_an_uncovered_nugget(evidence):
    inv = inventory()
    aligned = alignment(inv)
    aligned["matches"][0]["status"] = "contradicted"
    value, _ = validate_alignment(
        canonical(aligned), inv, build_windows(evidence, FeedbackConfig())[0]
    )
    assert value["matches"][0]["status"] == "contradicted"
    assert value["findings"] == []
    assert "fidelity_status" not in value


def test_later_result_does_not_credit_earlier_reply(evidence):
    inv = inventory()
    window = build_windows(evidence, FeedbackConfig())[0]
    window["sources"]["operation:op0"]["completed_media"] = 10
    value, errors = validate_alignment(canonical(alignment(inv)), inv, window)
    assert all(m["status"] == "uncertain" for m in value["matches"])
    assert errors


def test_end_of_audio_excludes_postcall_tool_result(evidence):
    evidence.source.update(scenario_start_time=1000, scenario_monotonic_origin=100)
    evidence.audio = {
        "timeline": [
            {"start": 0, "end": 5, "monotonic_start": 100, "monotonic_end": 105}
        ]
    }
    evidence.operations[0].ended = 1008
    evidence.operations[0].result = "POSTCALL_SECRET"
    window = build_windows(evidence, FeedbackConfig())[0]
    assert "POSTCALL_SECRET" not in canonical(generation_input(window, []))
    assert window["sources"]["operation:op0"]["completed_media"] is None


def test_calibration_file_is_resolved_and_fingerprinted(tmp_path):
    review_path = tmp_path / "reviews.json"
    review_path.write_text(json.dumps(reviews()))
    fitted = fit_calibration(review_path, "test", "Human development sample")
    (tmp_path / "calibration.json").write_text(json.dumps(fitted))
    config = tmp_path / "feedback.yaml"
    config.write_text(
        "measures: [conciseness]\nconciseness:\n  calibration: calibration.json\n"
    )
    loaded = MetricsConfig.load(config)
    assert loaded.conciseness.calibration.id == "test"
    assert loaded.fingerprint() != MetricsConfig(measures=["conciseness"]).fingerprint()


def test_provider_timeout_retries_and_audits_attempts(tmp_path, monkeypatch):
    from tau2.diagnostics.model import TauJsonModel
    from tau2.utils import llm_utils

    def fail(**kwargs):
        raise TimeoutError()

    monkeypatch.setattr(llm_utils, "generate", fail)
    requests = RequestBudget(2)
    with pytest.raises(TimeoutError):
        TauJsonModel(
            MetricsConfig(), "feedback_nuggets", tmp_path, requests
        ).complete_sync("test", {})
    assert requests.used == 2
    attempts = list((tmp_path / "attempts").glob("*.json"))
    assert len(attempts) == 2
    assert all(
        json.loads(path.read_text())["status"] == "provider_error" for path in attempts
    )


def test_audio_duration_uses_delivered_waveform_without_changing_text_score(
    evidence, tmp_path, monkeypatch
):
    from tau2.diagnostics.conciseness.evaluator import _audio_stats

    evidence.audio = {
        "status": "collected",
        "duration": 5,
        "timeline": [{"start": 3, "end": 4, "agent": {"bytes": 1}}],
    }
    evidence.audio["activity"] = {
        "agent_intervals": [[3, 3.5], [3.7, 4]],
        "user_turns": [],
    }

    def forbidden(*args):
        pytest.fail("conciseness must not invoke voice scoring")

    monkeypatch.setattr("tau2.diagnostics.voice.evaluate_voice", forbidden)
    window = build_windows(evidence, FeedbackConfig())[0]
    audio = _audio_stats(evidence, tmp_path, [window])[window["id"]]
    assert audio["agent_speech_seconds"] == pytest.approx(0.8)
    assert audio["agent_playback_seconds"] == 1


def test_calibrated_measure_roundtrip_reuses_successful_suite_result(
    evidence, tmp_path, monkeypatch
):
    from tau2.diagnostics.suite import evaluate_evidence, summarize

    calls = []

    class Model:
        def __init__(self, config, role, directory, budget):
            self.role, self.budget = role, budget

        def complete_sync(self, prompt, schema, *, validate):
            self.budget.take()
            calls.append(self.role)
            raw = canonical(
                inventory()
                if self.role == "feedback_nuggets"
                else alignment(inventory())
            )
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    config = MetricsConfig.model_validate(
        {
            "measures": ["conciseness"],
            "conciseness": {
                "language": "en",
                "calibration": {
                    "id": "test-only",
                    "reviewed": True,
                    "provenance": "Synthetic test fixture, not human benchmark results",
                    "review_sha256": "a" * 64,
                    "languages": {"en": budget().model_dump()},
                },
            },
        }
    )
    task = SimpleNamespace(model_dump=lambda **kw: {"id": "task"})
    first = evaluate_evidence(task, evidence, tmp_path / "evidence", config)
    second = evaluate_evidence(task, evidence, tmp_path / "evidence", config)
    assert len(calls) == 2
    assert first["summary"]["requests_this_run"] == 2
    assert second["summary"]["requests_this_run"] == 0
    measure = second["summary"]["measures"]["conciseness"]
    assert measure["trial"]["required_coverage"] == 1
    assert measure["trial"]["feedback_score"] is not None
    report = summarize([{**second, "task_id": "task", "reward": 0}])
    assert (
        report["metrics"]["conciseness"]["feedback_score"]
        == measure["trial"]["feedback_score"]
    )


def test_partial_review_has_no_implicit_human_approval(tmp_path):
    value = reviews()
    value["cases"][0]["reviews"][1]["approved"] = False
    path = tmp_path / "review.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="two approving"):
        fit_calibration(path, "must-fail", "Incomplete human review")


def test_collector_preserves_spoken_audio_without_transcript_as_unavailable():
    from tau2.data_model.message import AssistantMessage, Tick
    from tau2.data_model.transcript import LiveTranscriptTick
    from tau2.diagnostics.collect import delivered_inputs

    tick = Tick(
        tick_id=0,
        timestamp="2026-11-07T00:00:00",
        tick_duration_seconds=0.2,
        agent_chunk=AssistantMessage(
            role="assistant",
            contains_speech=True,
            raw_data={
                "live_transcript": LiveTranscriptTick(
                    session_id="s",
                    origin="recorded_audio",
                    alignment="played_audio",
                    speech_intervals_ms=[(0, 200)],
                    fragments=[],
                ).model_dump()
            },
        ),
    )
    rows = delivered_inputs(SimpleNamespace(ticks=[tick], info={}), None)
    assert len(rows) == 1
    assert rows[0].role == "assistant_context"
    assert rows[0].unavailable_reason
    assert not rows[0].text


def test_query_effect_does_not_treat_missing_asr_as_model_failure():
    from tau2.diagnostics.execution.gaia2 import Gaia2ExecutionAdapter

    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=[
            UserInput(
                id="missing-asr",
                role="assistant_context",
                text="",
                started=1,
                ended=2,
                stage=0,
                unavailable_reason="missing_delivered_agent_transcript",
            )
        ],
    )
    task = SimpleNamespace(
        scenario=SimpleNamespace(
            stages=[
                SimpleNamespace(
                    stage=0, criteria={"communicate_info": ["Required answer"]}
                )
            ]
        )
    )
    score = Gaia2ExecutionAdapter.query(task, evidence, None)
    assert score["score"] is None
    assert (
        score["units"][0]["checks"][0]["reason"]
        == "delivered_answer_transcript_unavailable"
    )


def test_query_still_ignores_explicitly_incomplete_reply():
    from tau2.diagnostics.execution.gaia2 import Gaia2ExecutionAdapter

    evidence = DiagnosticEvidence(
        simulation_id="s",
        task_id="t",
        domain="gaia2",
        user_inputs=[
            UserInput(
                id="cut",
                role="assistant_context",
                text="Required answer",
                started=1,
                ended=2,
                stage=0,
                complete=False,
            )
        ],
    )
    task = SimpleNamespace(
        scenario=SimpleNamespace(
            stages=[
                SimpleNamespace(
                    stage=0, criteria={"communicate_info": ["Required answer"]}
                )
            ]
        )
    )
    assert Gaia2ExecutionAdapter.query(task, evidence, None)["score"] == 0


def test_diagnostic_cache_identity_covers_shared_transcript_code(monkeypatch):
    from pathlib import Path

    from tau2.diagnostics.suite import implementation_digest

    before = implementation_digest()
    original = Path.read_bytes

    def altered(path):
        value = original(path)
        if path.as_posix().endswith("utils/transcript/live.py"):
            return value + b"\n# simulated transcript implementation change\n"
        return value

    monkeypatch.setattr(Path, "read_bytes", altered)
    assert implementation_digest() != before


def test_history_uses_the_same_unanimous_groups_as_coverage():
    inv = inventory(5)
    # A second spelling of g0 is a duplicate, while g1 has conflicting matches.
    inv["nuggets"].extend(
        [
            {**inv["nuggets"][0], "id": "alias0"},
            {**inv["nuggets"][1], "id": "alias1"},
        ]
    )
    aligned = alignment(inv)
    statuses = {
        "n2": "contradicted",
        "n3": "uncertain",
        "n4": "missing",
        "alias1": "uncertain",
    }
    for match in aligned["matches"]:
        match["status"] = statuses.get(match["nugget_id"], "matched")
    history = delivered_history(inv, aligned, "stage:0")
    assert history == [
        dict(
            window_id="stage:0",
            proposition="Required fact 0",
            equivalence_group="g0",
            evidence_refs=["operation:op0", "user:u0"],
        )
    ]
    score = score_window(inv, aligned, [{"text": "short"}], budget())
    assert score["required_total"] == 5
    assert score["required_matched"] == 1
    assert score["required_uncertain"] == 2


def test_expression_uncertainty_does_not_invalidate_matched_information(evidence):
    inv = inventory()
    aligned = alignment(inv)
    aligned["findings"] = [
        dict(
            kind="irrelevant",
            reason="Possibly padding",
            quotes=[dict(utterance_id="a0", text="unrecorded")],
            evidence_refs=[],
        )
    ]
    value, notes = validate_alignment(
        canonical(aligned), inv, build_windows(evidence, FeedbackConfig())[0]
    )
    assert notes and value["findings"][0]["kind"] == "uncertain"
    score = score_window(inv, value, [{"text": "short"}], budget())
    assert score["status"] == "measured"
    assert score["feedback_score"] == 100
    assert len(delivered_history(inv, value, "stage:0")) == 2


def test_legacy_frozen_inventory_is_reused_with_new_alignment(
    evidence, tmp_path, monkeypatch
):
    from tau2.diagnostics.conciseness.evaluator import _rubric

    config = MetricsConfig(measures=["conciseness"])
    window = build_windows(evidence, config.conciseness)[0]
    blind = generation_input(window, [])
    from tau2.diagnostics.conciseness.models import NuggetInventory

    key = digest(
        dict(
            prompt=_rubric("nuggets") + "\nINPUT:\n" + canonical(blind),
            schema=NuggetInventory.model_json_schema(),
            model=config.profile("feedback_nuggets").model_dump(),
            version=INVENTORY_VERSION,
        )
    )
    assert (
        INVENTORY_VERSION == "tau-feedback-conciseness/1"
        and VERSION != INVENTORY_VERSION
    )
    frozen = tmp_path / "conciseness-cache/nuggets" / f"{key}.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(
        canonical(
            dict(input_key=key, inventory=inventory(), sha256=digest(inventory()))
        )
    )
    original = frozen.read_bytes()
    calls = []

    class Model:
        def __init__(self, config, role, directory, budget):
            self.role = role

        def complete_sync(self, prompt, schema, *, validate):
            calls.append(self.role)
            assert self.role == "feedback_alignment"
            assert "fidelity_status" not in canonical(schema)
            raw = canonical(alignment(inventory()))
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    result = evaluate_feedback(
        evidence, config, tmp_path / "evidence", tmp_path / "out", RequestBudget(2)
    )
    assert calls == ["feedback_alignment"]
    assert result["version"] == VERSION
    assert result["trial"]["required_coverage"] == 1
    assert frozen.read_bytes() == original


def test_history_flows_to_next_window_without_truth_gate(
    evidence, tmp_path, monkeypatch
):
    evidence.user_inputs.extend(
        [
            UserInput(
                id="u1", text="Please repeat the result", started=10, ended=11, stage=1
            ),
            UserInput(
                id="a1",
                role="assistant_context",
                text="Repeated result",
                started=13,
                ended=14,
                stage=1,
            ),
        ]
    )
    evidence.stages["stages"].append(
        dict(stage=1, started={"time": 1010}, completed={"time": 1014})
    )
    evidence.operations.append(
        evidence.operations[0].model_copy(
            update=dict(call_id="op1", started=1011, ended=1012)
        )
    )
    seen_history = []

    class Model:
        def __init__(self, config, role, directory, budget):
            self.role = role

        def complete_sync(self, prompt, schema, *, validate):
            if self.role == "feedback_nuggets":
                data = json.loads(prompt.split("\nINPUT:\n", 1)[1])
                seen_history.append(data["validated_history"])
                inv = inventory()
                if data["window_id"] == "stage:1":
                    for item in inv["nuggets"]:
                        item["evidence_refs"] = ["user:u1", "operation:op1"]
                raw = canonical(inv)
            else:
                data = json.loads(prompt.split("\nINPUT:\n", 1)[1])
                inv = data["inventory"]
                reply = data["replies"][0]
                result = alignment(inv)
                for item in result["matches"]:
                    item["quotes"] = [
                        dict(utterance_id=reply["id"], text=reply["text"])
                    ]
                if data["context"]["window_id"] == "stage:0":
                    result["matches"][1]["status"] = "contradicted"
                raw = canonical(result)
            validate(raw)
            return raw

    monkeypatch.setattr("tau2.diagnostics.conciseness.evaluator.TauJsonModel", Model)
    report = evaluate_feedback(
        evidence,
        MetricsConfig(),
        tmp_path / "evidence",
        tmp_path / "out",
        RequestBudget(4),
    )
    assert report["status"] == "measured"
    assert report["trial"]["required_coverage"] == 0.75
    assert seen_history[0] == []
    assert [r["proposition"] for r in seen_history[1]] == ["Required fact 0"]
    assert seen_history[1][0]["window_id"] == "stage:0"
    assert seen_history[1][0]["evidence_refs"] == ["operation:op0", "user:u0"]
    assert "fidelity_status" not in canonical(report)


def test_no_reply_with_budget_and_optional_unknown_ranges():
    inv = inventory(2, 1)
    silent = score_window(inv, alignment(inv, "missing"), [], budget())
    assert (
        silent["required_coverage"]
        == silent["length_efficiency"]
        == silent["feedback_score"]
        == 0
    )
    aligned = alignment(inv)
    aligned["matches"][-1]["status"] = "uncertain"
    score = score_window(inv, aligned, [{"text": "x" * 100}], budget())
    assert score["required_coverage"] == 1
    assert score["length_efficiency"] is score["feedback_score"] is None
    assert 0 < score["feedback_score_lower"] < score["feedback_score_upper"] <= 100

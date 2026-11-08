"""Whole-call interaction scoring with fake judges and ordinary saved runs."""

import argparse
import json
from types import SimpleNamespace

import pytest

from tau2.config import DEFAULT_INTERACTION_EVENT_CHARACTERS
from tau2.data_model.diagnostics import (
    BackendInput,
    DiagnosticEvidence,
    OperationEvidence,
    UserInput,
)
from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.simulation import (
    Info,
    Results,
    RewardInfo,
    SimulationRun,
    TerminationReason,
    UserInfo,
)
from tau2.data_model.tasks import Task, UserScenario
from tau2.diagnostics.cli import add_metrics_args
from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.interaction import (
    build_input,
    evaluate_interaction,
    summarize_interaction,
)
from tau2.diagnostics.model import RequestBudget
from tau2.diagnostics.suite import (
    evaluate_evidence,
    read_saved_reports,
    run_saved,
    summarize,
)
from tau2.environment.environment import EnvironmentInfo


@pytest.fixture
def call_evidence():
    return DiagnosticEvidence(
        simulation_id="call",
        task_id="mail",
        domain="mock",
        user_inputs=[
            UserInput(id="u0", text="Send an email.", started=0, ended=1, stage=0),
            UserInput(
                id="a0",
                text="Received. Who is the recipient? What model is your computer?",
                started=2,
                ended=3,
                stage=0,
                role="assistant_context",
            ),
            UserInput(id="u1", text="Send it to Alex.", started=4, ended=5, stage=1),
            UserInput(
                id="a1",
                text="The email has been sent.",
                started=6,
                ended=7,
                stage=1,
                role="assistant_context",
            ),
            UserInput(id="u2", text="Thanks.", started=8, ended=9, stage=2),
            UserInput(
                id="a2",
                text="You're welcome.",
                started=10,
                ended=11,
                stage=2,
                role="assistant_context",
            ),
        ],
    )


def judge_output():
    records = [
        (
            "a0",
            "Received. Who is the recipient?",
            "clarification",
            "useful",
            "Obtains a missing recipient.",
        ),
        (
            "a0",
            "What model is your computer?",
            "clarification",
            "unhelpful",
            "Unrelated to sending the email.",
        ),
        (
            "a1",
            "The email has been sent.",
            "result_delivery",
            "uncertain",
            "No execution evidence confirms sending.",
        ),
        ("a2", "You're welcome.", "other", "useful", "Acknowledges closure."),
    ]
    return {
        "acts": [
            dict(zip(("utterance_id", "text", "type", "label", "reason"), record))
            for record in records
        ]
    }


@pytest.fixture
def judge(monkeypatch):
    calls = []
    responses = [judge_output()]

    def generate(**kwargs):
        calls.append(kwargs)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            content=response if isinstance(response, str) else json.dumps(response),
            usage={"input_tokens": 12, "output_tokens": 8},
            cost=0.01,
        )

    monkeypatch.setattr("tau2.utils.llm_utils.generate", generate)
    return calls, responses


def config(**kwargs):
    return MetricsConfig(measures=["interaction"], max_requests=1, **kwargs)


def test_one_call_splits_mixed_speech_and_covers_three_stages(
    call_evidence, judge, tmp_path
):
    calls, _ = judge
    budget = RequestBudget(1)
    result = evaluate_interaction(call_evidence, config(), tmp_path, budget)
    assert result["status"] == "complete"
    assert result["utterance_count"] == 3
    assert result["counts"] == {"useful": 2, "unhelpful": 1, "uncertain": 1}
    assert result["effective_interaction_rate"] == pytest.approx(2 / 3)
    assert result["judgment_coverage"] == 0.75
    assert len(result["acts"]) == 4
    assert budget.used == len(calls) == 1
    assert calls[0]["num_retries"] == calls[0]["response_validation_retries"] == 0
    assert calls[0]["call_name"] == "diagnostic_interaction"
    assert result["future_context_visible"] is True


def test_input_previews_large_events_without_hidden_state(call_evidence):
    large = "private data " * DEFAULT_INTERACTION_EVENT_CHARACTERS
    call_evidence.frames = {"secret": {"hidden_answer": large}}
    call_evidence.source["reward"] = 0
    call_evidence.operations = [
        OperationEvidence(
            call_id="tool0",
            name="email",
            app="mail",
            kind="WRITE",
            arguments={"body": large},
            raw_arguments={},
            result=large,
            started=100,
            ended=101,
        )
    ]
    call_evidence.backend_inputs = [
        BackendInput(id="internal", content="hidden delegation", time=0, internal=True),
        BackendInput(id="public", content=large, time=1, accepted=None),
    ]
    payload = build_input(call_evidence)
    assert len(payload["dialogue"]) == 6
    assert payload["target_utterance_ids"] == ["a0", "a1", "a2"]
    assert payload["operations"][0]["result"]["truncated"] is True
    assert len(payload["operations"][0]["result"]["preview"]) == (
        DEFAULT_INTERACTION_EVENT_CHARACTERS
    )
    assert len(payload["backend_inputs"]) == 1
    assert payload["backend_inputs"][0]["accepted"] is None
    assert "frames" not in payload and "source" not in payload
    assert "hidden_answer" not in json.dumps(payload)


@pytest.mark.parametrize("failure", ["missing", "unknown", "label", "json", "timeout"])
def test_failures_do_not_retry_or_cache_invalid_judgments(
    call_evidence, judge, tmp_path, failure
):
    calls, responses = judge
    invalid = judge_output()
    if failure == "missing":
        invalid["acts"].pop()
    elif failure == "unknown":
        invalid["acts"][0]["utterance_id"] = "not-an-utterance"
    elif failure == "label":
        invalid["acts"][0]["label"] = "excellent"
    elif failure == "json":
        invalid = "not JSON"
    else:
        invalid = TimeoutError("unreachable")
    responses[:] = [invalid, judge_output()]
    result = evaluate_interaction(call_evidence, config(), tmp_path, RequestBudget(1))
    assert result["status"] == "grader_error"
    assert "counts" not in result
    assert len(calls) == 1
    assert not list(tmp_path.rglob("*.json"))
    retried = evaluate_interaction(call_evidence, config(), tmp_path, RequestBudget(1))
    assert retried["status"] == "complete"
    assert len(calls) == 2


def test_empty_and_oversized_input_make_no_requests(call_evidence, judge, tmp_path):
    calls, _ = judge
    oversized = evaluate_interaction(
        call_evidence, config(max_characters=10), tmp_path, RequestBudget(1)
    )
    assert oversized["reason"] == "diagnostic_input_too_long"
    call_evidence.user_inputs = call_evidence.user_inputs[:1]
    empty = evaluate_interaction(call_evidence, config(), tmp_path, RequestBudget(1))
    assert empty["status"] == "not_applicable"
    assert empty["effective_interaction_rate"] is None
    assert not calls


def test_all_uncertain_and_all_unhelpful_are_distinct(call_evidence, judge, tmp_path):
    _, responses = judge
    for label in ("uncertain", "unhelpful"):
        output = judge_output()
        for act in output["acts"]:
            act["label"] = label
        responses[:] = [output]
        result = evaluate_interaction(
            call_evidence, config(), tmp_path / label, RequestBudget(1)
        )
        assert result["effective_interaction_rate"] == (
            None if label == "uncertain" else 0
        )
        assert result["judgment_coverage"] == (0 if label == "uncertain" else 1)


def test_cache_is_reused_and_invalid_trials_are_excluded(
    call_evidence, judge, tmp_path
):
    calls, _ = judge
    task = Task(id="mail", user_scenario=UserScenario(instructions="hidden task"))
    first = evaluate_evidence(task, call_evidence, tmp_path / "evidence", config())
    second = evaluate_evidence(task, call_evidence, tmp_path / "evidence", config())
    assert first["summary"]["requests_this_run"] == 1
    assert second["summary"]["requests_this_run"] == 0
    assert len(calls) == 1
    measure = second["summary"]["measures"]["interaction"]
    pooled = summarize_interaction(
        [measure, dict(measure, diagnostic_only=True), {"status": "grader_error"}, None]
    )
    assert pooled["scored"] == 1 and pooled["total_trials"] == 4
    assert pooled["counts"] == measure["counts"]
    report = summarize([first, {"profile": first["profile"]}])
    assert report["interaction"]["total_trials"] == 2
    assert report["interaction"]["effective_interaction_rate"] == pytest.approx(2 / 3)
    alternate = dict(measure, counts={"useful": 0, "unhelpful": 6, "uncertain": 0})
    assert summarize_interaction([measure, alternate])[
        "effective_interaction_rate"
    ] == pytest.approx(2 / 9)


def saved_run(path, domain="mock", format="json"):
    task = Task(
        id="mail", user_scenario=UserScenario(instructions="hidden user script")
    )
    simulation = SimulationRun(
        id="historic",
        task_id=task.id,
        start_time="2026-11-07T00:00:00",
        end_time="2026-11-07T00:01:00",
        duration=60,
        termination_reason=TerminationReason.USER_STOP,
        reward_info=RewardInfo(reward=0),
        trial=0,
        messages=[
            UserMessage(role="user", content="Send an email."),
            AssistantMessage(role="assistant", content="Who is the recipient?"),
        ],
    )
    Results(
        info=Info(
            git_commit="test",
            num_trials=1,
            max_steps=10,
            max_errors=1,
            user_info=UserInfo(implementation="user_simulator"),
            agent_info={"implementation": "llm_agent"},
            environment_info=EnvironmentInfo(domain_name=domain, policy="test"),
        ),
        tasks=[task],
        simulations=[simulation],
    ).save(path, format=format)


def test_historical_dialogue_cli_dry_run_cache_and_export(judge, tmp_path, capsys):
    calls, responses = judge
    path = tmp_path / "results.json"
    saved_run(path)
    original = path.read_bytes()
    parser = argparse.ArgumentParser()
    add_metrics_args(parser)
    config_path = tmp_path / "interaction.yaml"
    config_path.write_text("measures: [interaction]\nmax_requests: 1\n")
    args = parser.parse_args(
        [
            "run",
            str(path),
            "--config",
            str(config_path),
            "--only",
            "interaction",
            "--dry-run",
        ]
    )
    args.func(args)
    assert '"target_utterances": 1' in capsys.readouterr().out
    assert not calls and not (tmp_path / "artifacts").exists()
    responses[:] = [
        {
            "acts": [
                dict(
                    judge_output()["acts"][0],
                    utterance_id="1",
                    text="Who is the recipient?",
                )
            ]
        }
    ]
    first = run_saved(path, config())
    assert first["interaction"]["effective_interaction_rate"] == 1
    measure = first["records"][0]["summary"]["measures"]["interaction"]
    assert measure["evidence_mode"] == "conversation_only"
    prompt = calls[0]["messages"][0].content
    assert "hidden user script" not in prompt
    assert '"dialogue":"message_index"' in prompt
    assert len(calls) == 1
    assert read_saved_reports(path)["interaction"]["counts"] == measure["counts"]
    run_saved(path, config())
    assert len(calls) == 1
    export_path = tmp_path / "export.json"
    export_args = parser.parse_args(["export", str(path), "--output", str(export_path)])
    export_args.func(export_args)
    assert json.loads(export_path.read_text())["interaction"]["scored"] == 1
    assert len(calls) == 1 and path.read_bytes() == original


def test_historical_fallback_is_interaction_only_and_run_specific(
    judge, tmp_path, monkeypatch
):
    from tau2.diagnostics import interaction

    calls, _ = judge
    domains = []
    original = interaction.conversation_evidence

    def capture(sim, task, domain):
        domains.append(domain)
        return original(sim, task, domain)

    monkeypatch.setattr(interaction, "conversation_evidence", capture)
    for name in ("mock", "retail"):
        saved_run(tmp_path / name / "results.json", domain=name, format="dir")
    report = run_saved(tmp_path, config(), dry_run=True)
    assert len(report["records"]) == 2
    assert all(row["status"] == "planned" for row in report["records"]), report[
        "records"
    ]
    assert sorted(domains) == ["mock", "retail"]
    blocked = run_saved(tmp_path, config(), only=["interaction", "voice"])
    assert all(
        row["reason"] == "historical_run_missing_diagnostic_evidence"
        for row in blocked["records"]
    )
    assert not calls

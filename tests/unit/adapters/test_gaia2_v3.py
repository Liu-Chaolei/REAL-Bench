import asyncio
import json
import sys
import wave
from pathlib import Path

import pytest

RUNNER = (
    Path(__file__).resolve().parents[3]
    / "adapters/gaia2/qwen_audio_template/environment/runner"
)
sys.path.insert(0, str(RUNNER))
from audio_driver import AudioDriver, Playback, service_error_cause
from boundary import BoundaryAssessor, MeasurementError, parse_vote
from protocol import Protocol, ProtocolError
from quality import interaction_report

from adapters.gaia2.boundary_review import validate_examples
from adapters.gaia2.campaign_runner import run, stops_batch


def vote(kind, acts):
    return {"kind": kind, "acts": acts, "reason": "A controlled dialogue act"}


def assessor(kind, acts=None):
    return BoundaryAssessor(
        lambda messages: (
            json.dumps(
                vote(
                    kind,
                    acts
                    or {
                        "progress": ["progress"],
                        "failure": ["refusal"],
                        "clarification": ["clarification"],
                        "checkpoint": ["checkpoint"],
                        "final": ["answer"],
                    }[kind],
                )
            ),
            None,
        )
    )


@pytest.mark.parametrize(
    "kind", ["progress", "checkpoint", "final", "clarification", "failure"]
)
def test_two_independent_votes_and_no_reference_input(kind):
    calls = []

    def engine(messages):
        calls.append(messages)
        return json.dumps(
            vote(
                kind,
                {
                    "progress": ["progress"],
                    "checkpoint": ["checkpoint"],
                    "final": ["answer"],
                    "clarification": ["clarification"],
                    "failure": ["refusal"],
                }[kind],
            )
        ), None

    decision = BoundaryAssessor(engine).classify(
        "actual output", user_context=["heard input"]
    )
    assert len(calls) == 2
    assert decision.kind == kind
    body = json.loads(calls[0][1]["content"])
    assert set(body) == {"spoken", "user_context", "spoken_context"}
    assert body["spoken"] == "actual output"


@pytest.mark.parametrize(
    "first,second",
    [
        ("progress", "final"),
        ("failure", "progress"),
        ("clarification", "progress"),
        ("uncertain", "uncertain"),
    ],
)
def test_disagreement_never_becomes_a_model_failure(first, second):
    acts = {
        "progress": ["progress"],
        "final": ["answer"],
        "failure": ["refusal"],
        "clarification": ["clarification"],
        "uncertain": ["progress"],
    }
    replies = iter([vote(first, acts[first]), vote(second, acts[second])])
    engine = lambda messages: (json.dumps(next(replies)), None)
    with pytest.raises(MeasurementError):
        BoundaryAssessor(engine).classify("ambiguous utterance")


@pytest.mark.parametrize(
    "act", ["answer", "clarification", "refusal", "checkpoint", "completion_claim"]
)
def test_formal_content_cannot_hide_under_progress(act):
    with pytest.raises(MeasurementError):
        parse_vote(json.dumps(vote("progress", ["progress", act])))


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        "{}",
        json.dumps(vote("invalid", ["progress"])),
        json.dumps(vote("progress", ["unknown"])),
    ],
)
def test_malformed_assessment_invalidates_measurement(payload):
    with pytest.raises(MeasurementError):
        parse_vote(payload)


def test_classifier_provider_failure_is_measurement_error():
    def unavailable(messages):
        raise RuntimeError("upstream unavailable")

    with pytest.raises(MeasurementError, match="unavailable"):
        BoundaryAssessor(unavailable).classify("4")


def driver(tmp_path, kind):
    calls = []
    d = AudioDriver(
        "http://gateway",
        "dummy",
        tmp_path / "input.wav",
        tmp_path,
        list,
        lambda: (10, 1),
        calls.append,
        assessor=assessor(kind),
    )
    return d, calls


@pytest.mark.parametrize(
    "declared,actual,text,expected",
    [
        ("progress", "failure", "I cannot access your calendar.", True),
        ("progress", "clarification", "Which address should I use?", True),
        ("final", "progress", "I am checking the calendar.", False),
        ("progress", "final", "The answer is 4.", True),
        ("checkpoint", "checkpoint", "The first appointment is booked.", True),
    ],
)
async def test_actual_dialogue_act_controls_projection(
    tmp_path, declared, actual, text, expected
):
    d, calls = driver(tmp_path, actual)
    p = Playback(
        "r",
        pcm=bytearray(b"\x01\0" * 20),
        played_pcm=bytearray(b"\x01\0" * 20),
        transcript=text,
        ended=True,
        generation_done=True,
        delivery_kind=declared,
    )
    await d.maybe_record_reply(p)
    assert calls == ([text] if expected else [])
    assert d.protocol.turn == int(expected)


async def test_backend_correct_answer_does_not_replace_spoken_refusal(tmp_path):
    d, calls = driver(tmp_path, "failure")
    delivery = d.protocol.declare("backend", "final")
    delivery.requested_content = "4"
    p = Playback(
        "r",
        pcm=bytearray(b"\0\0"),
        played_pcm=bytearray(b"\0\0"),
        transcript="I need permission before I can answer.",
        ended=True,
        generation_done=True,
        delivery_id="backend",
        delivery_kind="final",
    )
    await d.maybe_record_reply(p)
    assert calls == ["I need permission before I can answer."]
    assert "4" not in calls


async def test_only_heard_samples_are_written_on_interruption(tmp_path):
    d, calls = driver(tmp_path, "final")

    async def send(event):
        pass

    d.send = send
    p = Playback(
        "r",
        pcm=bytearray(b"\x01\0" * 12000),
        transcript="long speech",
        generation_done=True,
    )
    p.chunks.put_nowait(bytes(p.pcm))
    p.chunks.put_nowait(None)
    async with asyncio.TaskGroup() as group:
        group.create_task(d.playback(p))
        await asyncio.sleep(0.035)
        p.cancelled = True
        import time

        p.cancelled_at = time.monotonic()
    d.write_audio(p, 1)
    with wave.open(str(tmp_path / "response-0001.wav")) as wav:
        assert 0 < wav.getnframes() < 12000
    with wave.open(str(tmp_path / "response-0001.generated.wav")) as wav:
        assert wav.getnframes() == 12000
    assert calls == []


def test_provisional_completion_can_become_waiting_but_closed_cannot(tmp_path):
    p = Protocol(tmp_path, lambda: (0, 1))
    p.set_state("completed")
    assert not p.can_finish(input_done=False, idle=True)
    p.set_state("waiting_environment")
    assert not p.can_finish(input_done=True, idle=True)
    p.set_state("waiting_user")
    assert p.can_finish(input_done=True, idle=True)
    with pytest.raises(ProtocolError):
        p.set_state("running")


@pytest.mark.parametrize(
    "message,cause",
    [
        ("Arrearage: account is not in good standing", "provider_billing"),
        ("余额不足", "provider_billing"),
        ("invalid_api_key", "provider_unavailable"),
        (
            "Your session was closed because no user input was received for 180 seconds.",
            "provider_inactivity",
        ),
        ("Cannot create response while user is speaking.", "response_while_speaking"),
    ],
)
def test_provider_causes_remain_distinct(message, cause):
    assert service_error_cause({"message": message}) == cause


@pytest.mark.parametrize(
    "status", ["completed", "protocol_failure", "execution_timeout"]
)
def test_model_failure_does_not_trigger_selective_rerun(status):
    assert not stops_batch(
        {"status": status, "measurement_valid": True, "success": False}
    )


@pytest.mark.parametrize(
    "status", ["infrastructure_error", "measurement_invalid", "user_aborted"]
)
def test_invalid_measurement_stops_future_admission(status):
    assert stops_batch({"status": status, "measurement_valid": False})


def test_unrestored_services_block_before_any_files_or_requests(tmp_path):
    with pytest.raises(ValueError, match="Restore DashScope"):
        run(tmp_path / "absent", "audio")
    assert not list(tmp_path.iterdir())


def test_unlabeled_examples_accept_integrity_but_detect_audio_changes(tmp_path):
    import hashlib

    audio = tmp_path / "response.wav"
    audio.write_bytes(b"recorded-audio")
    data = {
        "oracle_visible": False,
        "examples": [
            {
                "id": str(i),
                "split": "blind",
                "spoken": "Working on it.",
                "user_context": [],
                "spoken_context": [],
                "audio": str(audio),
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            }
            for i in range(60)
        ],
    }
    assert len(validate_examples(data)) == 60
    audio.write_bytes(b"changed")
    with pytest.raises(ValueError, match="missing or changed"):
        validate_examples(data)


def test_automatic_policy_does_not_require_human_audit():
    from adapters.gaia2.evaluation_campaign import check_gate

    manifest = {"seed_cases": [], "review_policy": "automatic"}
    report = check_gate(manifest, [], {}, {})
    assert not report["passed"]  # Missing automatic evidence still blocks expansion.
    assert not any(
        "Human" in message or "human" in message for message in report["failures"]
    )
    assert report["audio_transcript_semantics_verified"] is False
    assert report["boundary_semantic_accuracy"] is None


def test_quality_report_is_separate_and_keeps_content_difference(tmp_path):
    rows = [
        {"event": "delivery.requested", "delivery_id": "d", "content": "4"},
        {
            "event": "delivery.committed",
            "delivery_id": "d",
            "response_id": "r",
            "kind": "failure",
            "transcript": "I cannot answer.",
        },
    ]
    (tmp_path / "protocol.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    report = interaction_report(tmp_path)
    assert report["automatically_changes_task_score"] is False
    assert report["responses"][0]["spoken"] == "I cannot answer."
    assert report["responses"][0]["requested_content"] == "4"
    assert report["findings"][0]["type"] == "text_difference_requires_semantic_review"


def test_recovery_and_assessment_pauses_do_not_release_each_other(tmp_path):
    d, _ = driver(tmp_path, "final")
    d.set_transport_paused(True, "reconnect")
    d.set_transport_paused(True, "assessment")
    d.set_transport_paused(False, "assessment")
    assert not d.transport_gate.is_set()
    d.set_transport_paused(False, "reconnect")
    assert d.transport_gate.is_set()


def fake_campaign(tmp_path, monkeypatch, outcomes, stop_now=False):
    import hashlib

    import yaml

    from adapters.gaia2 import campaign_runner as runner
    from adapters.gaia2.qwen_audio_template.environment.runner.boundary import (
        PROMPT,
        PROMPT_VERSION,
    )

    manifest = {
        "protocol": "gaia2-qwen-audio/3",
        "profile": {"judge_model": "gpt-6-astra", "judge_effort": "low"},
        "seed_cases": [
            {"source_id": f"id-{i}", "task_name": f"task-{i}"}
            for i in range(len(outcomes))
        ],
    }
    (tmp_path / "manifest.json").write_text("{}")
    calibration = tmp_path / "boundary-calibration"
    calibration.mkdir()
    (calibration / "summary.json").write_text(
        json.dumps(
            {
                "passed": True,
                "examples": 60,
                "validation_mode": "automatic_stability",
                "resolved_examples": 60,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                "model": "gpt-6-astra",
                "effort": "low",
            }
        )
    )
    (tmp_path / "audio.yaml").write_text(
        yaml.safe_dump(
            {"jobs_dir": str(tmp_path / "jobs"), "datasets": [{"path": "unused"}]}
        )
    )
    monkeypatch.setattr(runner, "verify_freeze", lambda campaign: manifest)
    monkeypatch.setattr(
        "adapters.gaia2.boundary_review.verify_calibration",
        lambda campaign, profile: True,
    )
    launched = []

    class Process:
        pid = 987654321

        def __init__(self, args, **kwargs):
            config = yaml.safe_load(Path(args[-1]).read_text())
            launched.append(config)
            job = Path(config["jobs_dir"]) / config["job_name"]
            job.mkdir(parents=True)
            (job / "result.json").write_text(
                json.dumps(
                    {
                        "stats": {
                            "n_running_trials": 1,
                            "n_pending_trials": 0,
                            "n_cancelled_trials": 0,
                        }
                    }
                )
            )
            if stop_now:
                (tmp_path / "stop-requested").touch()
            else:
                for name in config["datasets"][0]["task_names"]:
                    index = int(name.split("-")[-1])
                    folder = job / name / "agent"
                    folder.mkdir(parents=True)
                    (folder / "gaia2_qwen_audio_result.json").write_text(
                        json.dumps(
                            {**outcomes[index], "audio": {"source_id": f"id-{index}"}}
                        )
                    )

        def poll(self):
            return None if stop_now else 0

        def wait(self):
            return -9 if stop_now else 0

    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(runner, "archive_stopped_job", lambda job, out: [])
    return runner, launched


def test_batch_stops_before_next_case_after_measurement_failure(tmp_path, monkeypatch):
    runner, launched = fake_campaign(
        tmp_path,
        monkeypatch,
        [
            {"status": "measurement_invalid", "measurement_valid": False},
            {"status": "completed", "measurement_valid": True, "success": True},
        ],
    )
    ledger = runner.run(tmp_path, "audio", True)
    assert len(launched) == 1
    assert ledger["state"] == "blocked_on_measurement_or_service"
    assert ledger["completed_in_stage"] == 1
    assert ledger["not_started_in_stage"] == 1


def test_wrong_answer_is_preserved_and_does_not_block_next_case(tmp_path, monkeypatch):
    runner, launched = fake_campaign(
        tmp_path,
        monkeypatch,
        [
            {"status": "completed", "measurement_valid": True, "success": False},
            {"status": "completed", "measurement_valid": True, "success": True},
        ],
    )
    ledger = runner.run(tmp_path, "audio", True)
    assert len(launched) == 2
    assert ledger["state"] == "stage_completed"
    assert ledger["results"][0]["result"]["success"] is False


def test_user_stop_updates_state_without_inventing_a_model_failure(
    tmp_path, monkeypatch
):
    runner, launched = fake_campaign(tmp_path, monkeypatch, [{}, {}], stop_now=True)
    ledger = runner.run(tmp_path, "audio", True)
    assert len(launched) == 1
    assert ledger["state"] == "stopped_by_user"
    assert ledger["results"] == []
    assert len(ledger["interrupted"]) == 1
    assert ledger["not_started_in_stage"] == 1
    job = Path(launched[0]["jobs_dir"]) / launched[0]["job_name"]
    assert (
        json.loads((job / "result.json").read_text())["stats"]["n_running_trials"] == 0
    )
    assert (job / "framework-result-before-stop.json").exists()


def test_disconnect_during_unfinished_input_cannot_be_scored_as_model_failure(tmp_path):
    from audio_driver import InfrastructureError

    d, _ = driver(tmp_path, "final")
    d.protocol.start()
    with pytest.raises(InfrastructureError) as exc:
        d.begin_recovery()
    assert exc.value.cause == "provider_input_interrupted"


async def test_pending_delivery_recovery_reuses_identity_but_not_an_application_action(
    tmp_path,
):
    d, _ = driver(tmp_path, "final")
    d.input_done = True
    d.protocol.declare("d", "final")
    d.delivery_requests["d"] = {
        "sessionId": "s",
        "deliveryId": "d",
        "kind": "final",
        "content": "4",
        "attemptId": 1,
    }
    sent = []

    async def control(path, payload=None):
        sent.append((path, payload))
        return {"ok": True}

    d.control = control
    d.begin_recovery()
    await d.restore_deliveries()
    assert sent == [
        (
            "deliver",
            {
                "sessionId": "s",
                "deliveryId": "d",
                "kind": "final",
                "content": "4",
                "attemptId": 2,
            },
        )
    ]
    assert d.protocol.deliveries["d"].attempt_id == "2"
    assert d.protocol.turn == 0


def test_formal_subtype_disagreement_does_not_invent_a_turn_boundary_error():
    from boundary import resolve_votes

    assert (
        resolve_votes(
            [vote("final", ["answer"]), vote("clarification", ["clarification"])]
        )
        == "clarification"
    )


def test_spoken_completion_does_not_cancel_registered_environment_wait(tmp_path):
    p = Protocol(tmp_path, lambda: (0, 1))
    p.set_state("waiting_environment")
    p.suggest_state("completed")
    assert not p.can_finish(input_done=True, idle=True)
    p.set_state("completed")
    assert p.can_finish(input_done=True, idle=True)


@pytest.mark.parametrize("disagree", [False, True])
def test_unlabeled_boundary_check_records_replay_without_accuracy_claim(
    tmp_path, monkeypatch, disagree
):
    import hashlib

    import httpx

    from adapters.gaia2 import boundary_review as review

    audio = tmp_path / "response.wav"
    audio.write_bytes(b"audio evidence")
    examples = tmp_path / "examples.json"
    examples.write_text(
        json.dumps(
            {
                "oracle_visible": False,
                "examples": [
                    {
                        "id": str(i),
                        "split": "blind",
                        "spoken": "Working on it.",
                        "user_context": [],
                        "spoken_context": [],
                        "audio": str(audio),
                        "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                    }
                    for i in range(60)
                ],
            }
        )
    )
    env = tmp_path / "profile.env"
    env.write_text(
        "GAIA2_JUDGE_MODEL=test-model\nGAIA2_JUDGE_ENDPOINT=https://example.invalid/v1\nGAIA2_JUDGE_API_KEY=test-key\n"
    )
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        kind, acts = (
            ("final", ["answer"])
            if disagree and calls == 2
            else ("progress", ["progress"])
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {"kind": kind, "acts": acts, "reason": "test"}
                                ),
                            }
                        ],
                    }
                ],
            },
        )

    original = review.ResponsesJudge
    monkeypatch.setattr(
        review,
        "ResponsesJudge",
        lambda *args, **kwargs: original(
            *args,
            **kwargs,
            client=httpx.Client(transport=httpx.MockTransport(respond)),
        ),
    )
    output = tmp_path / "boundary-calibration"
    if disagree:
        with pytest.raises(Exception, match="decisions disagree"):
            review.calibrate(examples, output, env, True)
        assert not (output / "summary.json").exists()
        return
    report = review.calibrate(examples, output, env, True)
    assert report["passed"] and calls == 120
    assert report["human_validated"] is False
    assert report["semantic_accuracy"] is None
    assert "missed_formal" not in report and "agreement" not in report
    assert review.verify_calibration(
        tmp_path, {"judge_model": "test-model", "judge_effort": "low"}
    )
    assert calls == 120  # Replay has made no new model requests.
    report["resolved_examples"] = 59
    (output / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="does not pass"):
        review.verify_calibration(
            tmp_path, {"judge_model": "test-model", "judge_effort": "low"}
        )


@pytest.mark.parametrize(
    "kind,acts",
    [
        ("checkpoint", ["progress", "checkpoint", "answer"]),
        ("final", ["answer"]),
        ("final", ["answer", "checkpoint", "completion_claim"]),
        ("clarification", ["answer", "checkpoint", "clarification"]),
        ("failure", ["answer", "checkpoint", "refusal"]),
    ],
)
def test_partial_answers_do_not_imply_whole_request_completion(kind, acts):
    assert parse_vote(json.dumps(vote(kind, acts)))["kind"] == kind
    with pytest.raises(MeasurementError):
        parse_vote(json.dumps(vote("progress", acts)))


def test_smoke10_is_bounded_and_contains_two_cases_per_category():
    from collections import Counter

    from adapters.gaia2.campaign_runner import SMOKE10_PREFIXES, selection

    categories = ["execution", "search", "adaptability", "time", "ambiguity"] * 2
    rows = [
        {"source_id": prefix + "fixture", "config": category}
        for prefix, category in zip(SMOKE10_PREFIXES, categories)
    ]
    rows.append({"source_id": "other", "config": "time"})
    selected = selection({"seed_cases": rows}, "smoke10")
    assert len(selected) == 10
    assert Counter(row["config"] for row, _ in selected) == dict.fromkeys(categories, 2)
    assert {track for _, track in selected} == {"audio"}


@pytest.mark.parametrize("reverse", [False, True])
async def test_confirmed_filtered_input_is_not_mistaken_for_an_unavailable_asr(
    tmp_path, reverse
):
    from audio_driver import InfrastructureError, InputFilteredError

    d, _ = driver(tmp_path, "final")
    events = [
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "input-1",
            "reason": "turn_invalid",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": "Background speech.",
        },
    ]
    if reverse:
        events.reverse()

    class Socket:
        async def __aiter__(self):
            for payload in events * 2:
                yield json.dumps(
                    {"type": "evaluation.provider_event", "payload": payload}
                )

    d.ws = Socket()
    d.done = True
    async with asyncio.TaskGroup() as group:
        await d.receive(group)
    assert (
        d.user_context == []
    )  # Never inject a discarded transcript into the agent context.
    assert d.confirmed_filtered_inputs == {"input-1"}
    assert isinstance(
        d.idle_failure(), InfrastructureError
    )  # Unfinished input stays unconfirmed.
    d.input_done = True
    assert isinstance(d.idle_failure(), InputFilteredError)
    assert d.protocol.turn == 0


def test_unmatched_or_blank_transcription_cannot_validate_filtered_input(tmp_path):
    from audio_driver import InfrastructureError

    d, _ = driver(tmp_path, "final")
    d.input_done = True
    d.observe_provider_input(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "a",
            "reason": "turn_invalid",
        }
    )
    d.observe_provider_input(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "b",
            "transcript": "other input",
        }
    )
    d.observe_provider_input(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "a",
            "transcript": "",
        }
    )
    assert not d.confirmed_filtered_inputs
    assert isinstance(d.idle_failure(), InfrastructureError)


@pytest.mark.parametrize(
    "message,cause",
    [
        ("grpc error: statusCode=504, Response stream timeout", "provider_asr_failure"),
        ("Account is not in good standing", "provider_billing"),
    ],
)
async def test_failed_asr_after_an_accepted_prefix_invalidates_measurement(
    tmp_path, message, cause
):
    from audio_driver import InfrastructureError

    d, _ = driver(tmp_path, "final")
    d.user_context.append("An earlier accepted segment")
    d.input_done = True

    class Socket:
        async def __aiter__(self):
            yield json.dumps(
                {
                    "type": "evaluation.provider_event",
                    "payload": {
                        "type": "conversation.item.input_audio_transcription.failed",
                        "item_id": "later-input",
                        "error": {"code": "transcription_failed", "message": message},
                    },
                }
            )

    d.ws = Socket()
    async with asyncio.TaskGroup() as group:
        with pytest.raises(InfrastructureError) as error:
            await d.receive(group)
    assert error.value.cause == cause
    assert d.protocol.turn == 0


def test_reviewed_service_failure_stays_in_ledger_and_only_remaining_cases_run(
    tmp_path, monkeypatch
):
    runner, launched = fake_campaign(
        tmp_path,
        monkeypatch,
        [
            {
                "status": "infrastructure_error",
                "measurement_valid": False,
                "success": None,
            },
            {"status": "completed", "measurement_valid": True, "success": False},
        ],
    )
    first = runner.run(tmp_path, "audio", True)
    assert len(launched) == 1
    assert first["state"] == "blocked_on_measurement_or_service"
    monkeypatch.setattr(
        runner, "reviewed_infrastructure", lambda campaign, entry, manifest: True
    )
    resumed = runner.run(tmp_path, "audio", True)
    assert len(launched) == 2
    assert resumed["state"] == "stage_completed"
    assert resumed["results"][0] == first["results"][0]
    assert resumed["results"][0]["result"]["measurement_valid"] is False
    assert len({r["source_id"] for r in resumed["results"]}) == 2


async def test_microphone_silence_continues_after_file_eof_without_changing_source(
    tmp_path,
):
    import base64
    import time

    d, _ = driver(tmp_path, "final")
    original = b"\x01\x00" * 160
    with wave.open(str(d.audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(original)
    sent = bytearray()
    idle_timestamps = []

    async def send(event):
        sent.extend(base64.b64decode(event["audio"]))
        if d.input_done:
            idle_timestamps.append(d.last_activity)
        if len(sent) >= len(original) + 32000 * 2.05:
            d.done = True  # A provider needing >1.5 s silence can now finalize.

    d.send = send
    started = time.monotonic()
    await d.stream_input()
    assert time.monotonic() - started >= 2
    assert sent[: len(original)] == original
    assert not any(sent[len(original) :])
    assert d.input_duration == 0.01
    assert d.input_done and d.quiet_frames_sent > 0
    assert len(set(idle_timestamps)) == 1
    assert d.active_transport == 0


async def test_stopped_input_is_never_marked_complete(tmp_path):
    d, _ = driver(tmp_path, "final")
    with wave.open(str(d.audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * 1600)

    async def send(event):
        d.stop_requested = True

    d.send = send
    await d.stream_input()
    assert d.input_done is False


def test_recovery_cannot_drop_an_unsettled_input_after_file_eof(tmp_path):
    from audio_driver import InfrastructureError

    d, _ = driver(tmp_path, "final")
    d.protocol.start()
    d.input_done = True
    d.observe_provider_input(
        {
            "type": "session.created",
            "session": {"input_audio_transcription": {"model": "asr"}},
        }
    )
    d.observe_provider_input(
        {"type": "input_audio_buffer.speech_started", "item_id": "pending"}
    )
    with pytest.raises(InfrastructureError) as error:
        d.begin_recovery()
    assert error.value.cause == "provider_input_interrupted"
    d.observe_provider_input(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "pending",
            "transcript": "Done",
        }
    )
    assert not d.pending_input_items


def test_provider_without_asr_is_not_blocked_waiting_for_asr_receipts(tmp_path):
    d, _ = driver(tmp_path, "final")
    d.observe_provider_input(
        {"type": "session.created", "session": {"input_audio_transcription": None}}
    )
    d.observe_provider_input(
        {"type": "input_audio_buffer.speech_started", "item_id": "audio-only"}
    )
    assert not d.pending_input_items

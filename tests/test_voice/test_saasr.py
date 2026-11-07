"""Continuous caller scene contracts without GPUs, TTS or live model calls."""

import json
import sys
import wave
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest

from tau2.data_model.message import AssistantMessage, UserMessage
from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.saasr import SaasrConfig
from tau2.data_model.voice import SynthesisConfig, VoiceSettings
from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator
from tau2.voice.saasr import SaasrSession, loop_audio
from tau2.voice.saasr_index import build_index
from tau2.voice.saasr_worker import SaasrWorkerError, Worker, WorkerPool


class FakePool:
    identities = [{"protocol": 1, "source_sha256": "fixture"}]

    def __init__(self):
        self.calls = []

    def submit(self, request, **kwargs):
        self.calls.append(request)
        future = Future()
        directory = Path(request["output_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        rate = 16000
        if request["op"] == "prepare":
            # Distinct nonconstant signals reveal cursor resets and accidental
            # rescaling. A long all-zero region would fail the waiting-tick test.
            n = np.arange(rate)
            for name, data in {
                "background": 0.015 * np.sin(n / 13) + 0.02,
                "noise": np.cos(n / 19) * 0.01,
                "active": np.ones(rate, dtype=bool),
            }.items():
                np.save(directory / f"{name}.npy", data)
            result = {
                "directory": str(directory),
                "sample_rate": rate,
                "initial_offset_samples": 2400,
                "snr_db": 12.0,
                "peak_dbfs": -1,
                "period_samples": rate,
            }
        else:
            duration = 1600 if len(request["text"]) < 20 else 4800
            wet = np.zeros(duration + 2400, dtype=np.float32)
            wet[:duration] = 0.12 * np.sin(np.arange(duration) / 17)
            wet[duration:] = 0.03 * np.exp(-np.arange(2400) / 180)
            np.save(directory / "foreground.npy", wet)
            result = {
                "path": str(directory / "foreground.npy"),
                "sample_rate": rate,
                "speech_samples": duration,
                "total_samples": len(wet),
            }
        future.set_result(result)
        return future


@pytest.fixture
def settings(tmp_path):
    return SaasrConfig(
        worker_commands=[[sys.executable, "unused.py"]],
        devices=["cpu"],
        model_dir=tmp_path / "model",
        scene_yaml=tmp_path / "scene.yaml",
    )


@pytest.fixture
def session(settings, tmp_path, monkeypatch):
    pool = FakePool()
    monkeypatch.setattr("tau2.voice.saasr.get_pool", lambda _: pool)
    session = SaasrSession(
        settings, settings.scene_yaml, tmp_path / "render", seed=42, language="English"
    )
    yield session, pool
    session.close()


def caller(session):
    return VoiceStreamingUserSimulator(
        tools=[],
        instructions="Request a refund.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=True,
        use_llm_backchannel=False,
        chunk_size=1600,
        tick_duration_seconds=0.1,
        saasr_session=session,
    )


def test_all_user_text_routes_share_saasr_and_background_is_prepared_once(
    session, monkeypatch
):
    scene, pool = session
    user = caller(scene)
    state = user.get_init_state()
    try:
        scripted, state = user._generate_full_duplex_voice_message(
            None, state, ("initial", "Original request")
        )
        monkeypatch.setattr(
            "tau2.user.user_simulator_streaming.generate",
            lambda **kw: AssistantMessage(
                role="assistant",
                content="Yes, please.",
                cost=0,
                generation_time_seconds=0.1,
            ),
        )
        reply, state = user._generate_full_duplex_voice_message(None, state)
        backchannel = user._generate_backchannel_message(state)
        assert [c["text"] for c in pool.calls if c["op"] == "foreground"] == [
            "Original request",
            "Yes, please.",
            backchannel.content,
        ]
        assert sum(c["op"] == "prepare" for c in pool.calls) == 1
        assert scripted.raw_data["scenario_input_id"] == "initial"
        assert reply.audio_script_gold == "Yes, please."
        assert scene.position == 0 and not scene.started
    finally:
        user.stop(state=state)


def test_captions_and_speech_flags_exclude_background_and_tail(session):
    scene, _ = session
    user = caller(scene)
    state = user.get_init_state()
    try:
        message = user.synthesize_voice(
            UserMessage(role="user", content="Hello."), state
        )
        chunks = user._create_chunk_messages(message)
        assert chunks[0].content == "" and not chunks[0].contains_speech
        assert "".join(c.content for c in chunks) == "Hello."
        assert chunks[-1].content == "" and not chunks[-1].contains_speech
        assert sum(bool(c.raw_data.get("saasr_main_final")) for c in chunks) == 1
        for chunk in chunks:
            user._mix_saasr_chunk(chunk, state)
        position = scene.position
        # No main speech while waiting, but audio and background keep moving.
        waiting = user._mix_saasr_chunk(None, state)
        assert scene.position == position + 1600
        assert not waiting.contains_speech and waiting.content == ""
        assert any(waiting.get_audio_bytes())
        second = user.synthesize_voice(
            UserMessage(role="user", content="A much longer second response."), state
        )
        later = user._create_chunk_messages(second)
        assert later[0].contains_speech  # No initial offset repeated.
        assert scene.position == position + 1600  # Generation never seeks playback.
    finally:
        user.stop(state=state)


def test_background_loop_is_chunk_invariant_and_has_no_padded_silence():
    audio = np.sin(np.arange(800) / 31)
    whole = loop_audio(audio, 0, 5000, 100)
    parts = np.concatenate(
        [loop_audio(audio, p, min(137, 5000 - p), 100) for p in range(0, 5000, 137)]
    )
    np.testing.assert_array_equal(whole, parts)
    assert np.max(np.abs(np.diff(whole))) < 0.15
    assert np.count_nonzero(whole[-800:]) > 790


def test_discarded_generation_does_not_start_or_rewind_scene(session):
    scene, _ = session
    scene.synthesize("A discarded reply")
    assert not scene.started and scene.position == 0 and scene.noise_gain is None
    user = caller(scene)
    state = user.get_init_state()
    try:
        msg = user.synthesize_voice(UserMessage(role="user", content="Accepted"), state)
        chunks = user._create_chunk_messages(msg)
        user._mix_saasr_chunk(chunks[0], state)
        pos, gain = scene.position, scene.noise_gain
        scene.synthesize("Discarded after new input")
        assert scene.position == pos and scene.noise_gain == gain
    finally:
        user.stop(state=state)


def test_audio_recordings_are_finalized_even_after_cancellation(session):
    scene, _ = session
    user = caller(scene)
    state = user.get_init_state()
    msg = user.synthesize_voice(UserMessage(role="user", content="Hello"), state)
    chunks = user._create_chunk_messages(msg)
    for c in chunks:
        emitted = user._mix_saasr_chunk(c, state)
        scene.record_sent(emitted)
    user.stop(state=state)
    with wave.open(str(scene.directory / "sent_to_agent.wav")) as audio:
        assert audio.getnframes() > 0
    assert (
        json.loads((scene.directory / "render.json").read_text())["played_samples"] > 0
    )


def test_peak_gain_is_shared_and_only_sent_ticks_are_recorded(session):
    from tau2.data_model.audio import audio_bytes_to_string
    from tau2.voice.saasr import SAASR_AUDIO_FORMAT

    scene, _ = session
    # A high foreground peak is temporarily attenuated for PCM transport, then
    # restored before common peak protection. Background ratios must survive.
    message = UserMessage(
        role="user",
        content="Hi",
        is_audio=True,
        audio_script_gold="Hi",
        audio_content=audio_bytes_to_string(
            np.full(3200, 16000, dtype="<i2").tobytes()
        ),
        audio_format=SAASR_AUDIO_FORMAT,
        raw_data={
            "saasr_foreground": {
                "transport_gain": 0.2,
                "speech_samples": 3200,
                "utterance": 0,
            }
        },
    )
    scene.commit(message)
    audio, token = scene.mix_tick(message, 3200)
    message.audio_content = audio_bytes_to_string(audio)
    message.raw_data["saasr_output_id"] = token
    scene.record_sent(message)
    # This last emitted tick is never handed to the Agent.
    scene.mix_tick(None, 3200)
    scene.close()
    tracks = {}
    for name in ("main", "background", "noise"):
        with (scene.directory / f"played_{name}.wav").open("rb") as handle:
            handle.seek(44)
            tracks[name] = np.frombuffer(handle.read(), dtype="<f4")
        assert len(tracks[name]) == 3200
    with wave.open(str(scene.directory / "played_mixed.wav")) as handle:
        mixed = (
            np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2") / 32767
        )
    np.testing.assert_allclose(sum(tracks.values()), mixed, atol=1 / 32767)
    assert np.max(np.abs(mixed)) <= 10 ** (-1 / 20) + 1 / 32767
    assert scene.record()["played_samples"] == 3200


def test_config_disabled_compatibility_and_explicit_async(settings):
    old = AudioNativeConfig()
    assert "user_audio_renderer" not in old.model_dump()
    enabled = AudioNativeConfig(user_audio_renderer="saasr", saasr_config=settings)
    assert enabled.realtime_generation_enabled
    with pytest.raises(ValueError, match="non-blocking"):
        AudioNativeConfig(
            user_audio_renderer="saasr",
            saasr_config=settings,
            realtime_generation=False,
        )


def test_index_is_generic_and_reports_all_missing_tasks(tmp_path, settings):
    scene = tmp_path / "scenes" / "category" / "retail-task" / "generated_scene.yaml"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}")
    index_path = tmp_path / "index.json"
    index = build_index(tmp_path / "scenes", "retail", index_path)
    index_path.write_text(json.dumps(index))
    cfg = settings.model_copy(update={"scene_yaml": None, "scene_index": index_path})
    assert cfg.scene_for("retail", "retail-task") == scene
    cfg.model_dir.mkdir()
    (cfg.model_dir / "fixture.safetensors").write_bytes(b"fixture weights")
    with pytest.raises(ValueError) as error:
        cfg.preflight("retail", ["missing1", "missing2"])
    assert "missing1" in str(error.value) and "missing2" in str(error.value)


def test_worker_jsonl_and_exit_failures_are_not_retried(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("""import sys,json
for line in sys.stdin:
    r=json.loads(line)
    if r['op']=='exit': sys.exit(2)
    print(json.dumps({'id':r['id'],'result':{'op':r['op']}}),flush=True)
""")
    worker = Worker([sys.executable, str(script)], "cpu", tmp_path / "logs")
    try:
        assert worker.submit({"op": "hello"}, 2).result(timeout=3) == {"op": "hello"}
        with pytest.raises(SaasrWorkerError, match="exited"):
            worker.submit({"op": "exit"}, 2).result(timeout=3)
    finally:
        worker.close()
    assert worker.process.poll() is not None


def test_worker_timeout_and_queued_cancellation(tmp_path):
    import threading

    script = tmp_path / "worker.py"
    script.write_text("""import sys,json,time
for line in sys.stdin:
    r=json.loads(line)
    time.sleep(2)
    print(json.dumps({'id':r['id'],'result':{}}),flush=True)
""")
    worker = Worker([sys.executable, str(script)], "cpu", tmp_path / "logs")
    try:
        pending = worker.submit({"op": "slow"}, 0.1)
        assert threading.Event().wait(0.01) is False
        queued = worker.submit({"op": "never"}, 1)
        assert queued.cancel()
        with pytest.raises(SaasrWorkerError, match="timed out"):
            pending.result(timeout=3)
        assert queued.cancelled()
    finally:
        worker.close()


def test_pool_cleanup_finalizes_calls_left_by_startup_failure(tmp_path, settings):
    script = tmp_path / "worker.py"
    script.write_text("""import sys,json
for line in sys.stdin:
    r=json.loads(line)
    print(json.dumps({'id':r['id'],'result':{'protocol':1}}),flush=True)
""")
    config = settings.model_copy(
        update={"worker_commands": [[sys.executable, str(script)]]}
    )
    pool = WorkerPool(config)

    class UnfinishedCall:
        closed = False

        def close(self):
            self.closed = True
            pool.release_session(self)

    call = UnfinishedCall()
    pool.register_session(call)
    pool.close()
    assert call.closed
    assert all(w.process.poll() is not None for w in pool.workers)


def test_parallel_sessions_have_independent_cursors_and_calibration(
    settings, tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    pool = FakePool()
    monkeypatch.setattr("tau2.voice.saasr.get_pool", lambda _: pool)
    scenes = [
        SaasrSession(
            settings, settings.scene_yaml, tmp_path / str(i), seed=i, language="English"
        )
        for i in range(2)
    ]
    users = [caller(s) for s in scenes]
    states = [u.get_init_state() for u in users]
    try:

        def speak(i):
            msg = users[i].synthesize_voice(
                UserMessage(role="user", content=f"Hello {i}"), states[i]
            )
            for chunk in users[i]._create_chunk_messages(msg):
                users[i]._mix_saasr_chunk(chunk, states[i])

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(speak, range(2)))
        old = scenes[1].position
        users[0]._mix_saasr_chunk(None, states[0])
        assert scenes[1].position == old
        users[0].stop(state=states[0])
        users[1]._mix_saasr_chunk(None, states[1])
        assert scenes[1].position == old + 1600
    finally:
        for u, s in zip(users, states):
            u.stop(state=s)


def test_non_gaia2_orchestrator_preserves_transcript_and_mixed_background(session):
    import time

    from tau2.data_model.message import TurnTakingAction
    from tau2.data_model.tasks import Task, UserScenario
    from tau2.domains.mock.environment import get_environment
    from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator

    scene, _ = session
    user = caller(scene)
    user.queue_scenario_input("request", "Refund please.")

    # Deterministic no-API scheduling: speak the scripted request once, then listen.
    def decision(state):
        if state.output_streaming_queue:
            return TurnTakingAction(action="keep_talking")
        if state.user_utterance_count == 0:
            return TurnTakingAction(
                action="generate_message", generation_origin="scripted"
            )
        return TurnTakingAction(action="wait")

    user._next_turn_taking_action = decision

    class Agent:
        received = []

        def set_seed(self, seed):
            pass

        def get_init_state(self, **kw):
            return None

        def get_next_chunk(self, state, participant_chunk=None, **kw):
            self.received.append(participant_chunk)
            time.sleep(0.002)
            text = "###STOP###" if len(self.received) >= 16 else ""
            return AssistantMessage(
                role="assistant", content=text, contains_speech=False
            ), state

        @staticmethod
        def is_stop(message):
            return message.content == "###STOP###"

        def stop(self, *args, **kw):
            pass

    agent = Agent()
    orchestrator = FullDuplexOrchestrator(
        "mock",
        agent,
        user,
        get_environment(),
        Task(
            id="audio-fixture",
            user_scenario=UserScenario(instructions="Refund please."),
        ),
        max_steps=20,
        tick_duration_seconds=0.1,
    )
    result = orchestrator.run()
    assert result.termination_reason.value == "agent_stop"
    assert "".join(c.content or "" for c in agent.received if c) == "Refund please."
    background = [
        c
        for c in agent.received
        if c and not c.contains_speech and c.has_audio_content()
    ]
    assert any(any(c.get_audio_bytes()) for c in background)
    assert result.info["user_audio_renderer"]["played_samples"] > 0


def test_renderer_failure_retains_partial_trajectory(session):
    from tau2.data_model.tasks import Task, UserScenario
    from tau2.domains.mock.environment import get_environment
    from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator

    scene, _ = session
    user = caller(scene)

    class Agent:
        def get_init_state(self, **kw):
            return None

        def get_next_chunk(self, state, **kw):
            return AssistantMessage(
                role="assistant", content="", contains_speech=False
            ), state

        def is_stop(self, message):
            return False

        def stop(self, *args, **kw):
            pass

    orchestrator = FullDuplexOrchestrator(
        "mock",
        Agent(),
        user,
        get_environment(),
        Task(id="failure-fixture", user_scenario=UserScenario(instructions="Hello")),
        max_steps=5,
    )
    original = user.get_next_chunk
    calls = 0

    def fail(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(*args, **kw)
        raise SaasrWorkerError("fixture TTS unavailable")

    user.get_next_chunk = fail
    result = orchestrator.run()
    assert result.termination_reason.value == "infrastructure_error"
    assert len(result.ticks) >= 2
    assert "fixture TTS unavailable" in result.info["audio_renderer_error"]

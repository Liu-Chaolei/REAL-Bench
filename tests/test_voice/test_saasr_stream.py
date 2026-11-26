"""Mechanism tests for sentence-sized SAASR first-audio release."""

import time
from threading import Event

from tau2.voice.saasr import SaasrSession, SaasrStream


class FakeSaasrSession:
    def __init__(self):
        self.calls = []
        self.block_after_first = Event()

    def synthesize(self, text):
        self.calls.append(text)
        if len(self.calls) > 1:
            self.block_after_first.wait(1)
        time.sleep(0.02)
        return text.encode(), {
            "utterance": len(self.calls) - 1,
            "speech_samples": len(text),
            "transport_gain": 1.0,
        }


def test_saasr_stream_releases_first_segment_before_completion():
    session = FakeSaasrSession()
    stream = SaasrStream(session, ["first", "second"])
    stream.start()

    first = stream.wait_first(timeout=1)

    assert first["text"] == "first"
    assert first["final"] is False
    assert stream.completed.is_set() is False
    session.block_after_first.set()
    while not stream.completed.is_set():
        time.sleep(0.01)
    second, done = stream.poll()
    assert second["text"] == "second"
    assert done is False
    assert stream.poll() == (None, True)
    stream.close()


def test_saasr_stream_cancels_queued_segments():
    session = FakeSaasrSession()
    stream = SaasrStream(session, ["first", "second", "third"])
    stream.start()
    assert stream.wait_first(timeout=1)["text"] == "first"
    stream.cancel()
    stream.close()
    assert stream.cancelled.is_set()


def test_saasr_stream_cancel_wakes_first_audio_waiter():
    """Cancelling before the first segment cannot leave a blocking waiter."""

    class Session:
        def synthesize(self, text):
            time.sleep(1)
            return b"\0\0", {"speech_samples": 1, "transport_gain": 1.0}

    stream = SaasrStream(Session(), ["first"])
    stream.start()
    stream.cancel()
    assert stream.wait_first(timeout=0.1) is None
    stream.close()


def test_saasr_foreground_can_use_standard_qwen_tts_callback(tmp_path):
    session = object.__new__(SaasrSession)
    session.counter = iter(range(10))
    session.foreground_synthesizer = lambda text: (
        b"\x10\x00" * 160,
        {"provider": "qwen", "sample_rate": 16000},
    )
    session.events = []
    session.directory = tmp_path
    session.cancelled = __import__("threading").Event()
    session.request = lambda *args, **kwargs: None

    audio, metadata = session.synthesize("hello")

    assert len(audio) == 320
    assert metadata["provider"] == "qwen"
    assert metadata["speech_samples"] == 160
    assert metadata["transport_gain"] == 1.0


def test_no_realtime_saasr_consumes_following_segments(monkeypatch):
    """Synchronous first audio must not bypass the remaining sentence queue."""
    from types import SimpleNamespace

    from tau2.data_model.message import TurnTakingAction, UserMessage
    from tau2.data_model.voice import SynthesisConfig, VoiceSettings
    from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator

    user = VoiceStreamingUserSimulator(
        tools=None,
        instructions="Ask for help.",
        llm="unused",
        voice_settings=VoiceSettings(
            transcription_config=None, synthesis_config=SynthesisConfig()
        ),
        realtime_generation=False,
        saasr_session=SimpleNamespace(),
        use_llm_backchannel=False,
    )
    state = user.get_init_state()
    queued = UserMessage(role="user", content="next sentence")
    items = iter([(None, False), ({"text": "next sentence"}, False)])
    user._saasr_stream = SimpleNamespace(poll=lambda: next(items), base_raw_data={})
    monkeypatch.setattr(user, "_create_chunk_messages", lambda _: [queued])
    monkeypatch.setattr(
        user,
        "_compute_turn_taking_action",
        lambda s: TurnTakingAction(action="keep_talking" if s.is_talking else "wait"),
    )
    assert user._next_turn_taking_action(state).info == "SAASR audio pending"
    # A completed segment is queued through the existing message constructor.
    items = iter(
        [
            (
                {
                    "text": "next sentence",
                    "audio": b"\0\0",
                    "metadata": {},
                    "final": True,
                },
                False,
            )
        ]
    )
    user._saasr_stream.poll = lambda: next(items)
    assert user._next_turn_taking_action(state).action == "keep_talking"
    assert state.output_streaming_queue == [queued]


def test_first_audio_gate_pauses_only_while_waiting():
    """Scenario time is resumed before the caller handles later chunks."""
    from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator

    class Runtime:
        def __init__(self):
            self.events = []

        def pause(self):
            self.events.append("pause")

        def resume(self):
            self.events.append("resume")

    class Stream:
        def wait_first(self):
            runtime.events.append("wait")
            return {"text": "first"}

    runtime = Runtime()
    user = object.__new__(VoiceStreamingUserSimulator)
    user._saasr_scenario_runtime = runtime

    result = user._wait_for_saasr_first_audio(Stream())

    assert result["text"] == "first"
    assert runtime.events == ["pause", "wait", "resume"]

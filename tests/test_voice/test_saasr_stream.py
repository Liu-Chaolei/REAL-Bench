"""Mechanism tests for sentence-sized SAASR first-audio release."""

import time
from threading import Event

from tau2.voice.saasr import SaasrStream


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

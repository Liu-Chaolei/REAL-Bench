import io
import wave

from tau2.data_model.voice import QwenTTSConfig
from tau2.voice import qwen_tts


def wav(rate=24000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x20\x01" * rate)
    return stream.getvalue()


class Response:
    def __init__(self, *, body=None, content=b"", status_code=200):
        self._body = body
        self.content = content
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._body


def test_qwen_tts_url_maps_compatible_base():
    assert qwen_tts.qwen_tts_url(
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ) == ("https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer")


def test_qwen_tts_downloads_wav_and_normalizes_to_tau_pcm(monkeypatch, tmp_path):
    env = tmp_path / "config.env"
    env.write_text(
        "QWEN_API_KEY=qwen-private\n"
        "QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
    )
    requests = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 60

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            return Response(
                body={"output": {"audio": {"url": "https://audio.invalid/result.wav"}}}
            )

        def get(self, url):
            assert url == "https://audio.invalid/result.wav"
            return Response(content=wav())

    monkeypatch.setattr(qwen_tts.httpx, "Client", Client)
    audio = qwen_tts.synthesize_qwen(
        "Hello.",
        QwenTTSConfig(config_env_file=str(env), voice_id="Andy_v3.1"),
    )

    assert audio.format.sample_rate == 16000
    assert audio.format.is_pcm16
    assert len(audio.data) == 32000
    assert requests[0][0].endswith("/SpeechSynthesizer")
    assert requests[0][1] == {"Authorization": "Bearer qwen-private"}
    assert requests[0][2] == {
        "model": "qwen-audio-3.1-tts-flash",
        "input": {
            "text": "Hello.",
            "voice": "Andy_v3.1",
            "format": "wav",
            "sample_rate": 24000,
            "language_type": "English",
        },
    }

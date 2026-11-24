"""Key-free, offline checks of the selected upstream runtime and native models."""

import os
from importlib.metadata import version
from pathlib import Path


def main():
    """Import the real pipeline and construct native VAD models without cloud calls."""
    import nltk
    import torch
    from server import pipeline_document
    from speech_to_speech.config import ConfigDocument, resolve_config, validate_config
    from speech_to_speech.configured_runtime import (
        build_configured_server,  # noqa: F401
    )
    from speech_to_speech.STT.openai_compatible_handler import (
        OpenAICompatibleSTTHandler,  # noqa: F401
    )
    from speech_to_speech.TTS.openai_compatible_handler import (
        OpenAICompatibleTTSHandler,  # noqa: F401
    )
    from speech_to_speech.VAD.smart_turn import SmartTurnAnalyzer

    assert version("speech-to-speech") == "1.1.0"
    assert torch.version.cuda is None
    nltk.data.find("tokenizers/punkt_tab")
    nltk.data.find("taggers/averaged_perceptron_tagger_eng")
    model, _ = torch.hub.load(
        "snakers4/silero-vad:master",
        "silero_vad",
        trust_repo=True,
        skip_validation=True,
    )
    assert model(torch.zeros(512), 16000).numel() == 1
    SmartTurnAnalyzer(model_path="/opt/s2s-assets/smart-turn-v3.2-cpu.onnx")
    settings = {
        "asr_model": "qwen-audio-3.1-asr-flash",
        "tts_model": "mimo-v2.5-tts",
        "voice": "Dean",
        "language": "en",
        "llm_api": "responses-api",
        "reasoning_effort": "low",
        "request_timeout_seconds": 60,
    }
    env = {
        "TAU_S2S_LLM_API_KEY": "fake",
        "TAU_S2S_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "TAU_S2S_LLM_MODEL": "frontend-test",
        "TAU_S2S_AUDIO_TOKEN": "local",
    }
    document = ConfigDocument(
        Path("/opt/tau-bridge/speech/pipeline.yaml"), pipeline_document(settings, env)
    )
    validate_config(document)
    resolve_config(document, environ=os.environ)
    print("TAU_S2S_INSTALL_VERIFIED", flush=True)


if __name__ == "__main__":
    main()

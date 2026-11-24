"""Launch stock Speech-to-Speech with a private ASR and TTS gateway."""

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from llm_fallback import install_llm_fallback
from mimo_gateway import create_app


def pipeline_document(settings: dict, env: dict) -> dict:
    """Select native backends without changing their prompts or tool semantics."""
    audio_url = "http://127.0.0.1:8766/v1"
    fallback = bool(env.get("TAU_S2S_LLM_FALLBACK_API_KEY"))
    return {
        "schema_version": 1,
        "runtime": {
            "log_level": "warning",
            "log_transcripts": False,
            "torch_num_threads": 1,
            "server": {"host": "127.0.0.1", "port": 8765},
        },
        "blocks": {
            "vad": {
                "kind": "vad",
                "backend": "silero",
                "settings": {
                    "smart_turn_model_path": "/opt/s2s-assets/smart-turn-v3.2-cpu.onnx",
                },
            },
            "asr": {
                "kind": "stt",
                "backend": "openai",
                "settings": {
                    "openai_stt_base_url": audio_url,
                    "openai_stt_api_key": env["TAU_S2S_AUDIO_TOKEN"],
                    "openai_stt_model": settings["asr_model"],
                    "openai_stt_language": settings["language"],
                    "openai_stt_timeout": settings["request_timeout_seconds"],
                },
            },
            "llm": {
                "kind": "llm",
                "backend": settings["llm_api"],
                "settings": {
                    "model_name": env["TAU_S2S_LLM_MODEL"],
                    "responses_api_base_url": audio_url
                    if fallback
                    else env["TAU_S2S_LLM_BASE_URL"],
                    "responses_api_api_key": env["TAU_S2S_AUDIO_TOKEN"]
                    if fallback
                    else env["TAU_S2S_LLM_API_KEY"],
                    "responses_api_stream": True,
                    "responses_api_reasoning_effort": settings["reasoning_effort"],
                    "stream_batch_sentences": 1,
                },
            },
            "tts": {
                "kind": "tts",
                "backend": "openai",
                "settings": {
                    "openai_tts_base_url": audio_url,
                    "openai_tts_api_key": env["TAU_S2S_AUDIO_TOKEN"],
                    "openai_tts_model": settings["tts_model"],
                    "openai_tts_voice": settings["voice"],
                    "openai_tts_response_format": "pcm",
                    "openai_tts_sample_rate": 24000,
                    "openai_tts_timeout": settings["request_timeout_seconds"],
                },
            },
        },
        "pipelines": {
            "voice": {
                "num_pipelines": 1,
                "stages": {"vad": "vad", "stt": "asr", "llm": "llm", "tts": "tts"},
                "options": {
                    "device": "cpu",
                    "enable_live_transcription": False,
                    "enable_llm_proxy": False,
                },
            }
        },
    }


def main():
    """Own the audio server and native runtime in the existing filesystem boundary."""
    from speech_to_speech.config import ConfigDocument, resolve_config, validate_config
    from speech_to_speech.configured_runtime import build_configured_server

    settings = json.loads(os.environ["TAU_S2S_CONFIG"])
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.basicConfig(level=logging.WARNING)
    app = create_app(
        settings,
        os.environ["TAU_S2S_MIMO_API_KEY"],
        os.environ["TAU_S2S_AUDIO_TOKEN"],
        asr_key=os.environ.get("TAU_S2S_ASR_API_KEY"),
        asr_base_url=os.environ.get("TAU_S2S_ASR_BASE_URL"),
    )
    if os.environ.get("TAU_S2S_LLM_FALLBACK_API_KEY"):
        install_llm_fallback(
            app,
            os.environ["TAU_S2S_AUDIO_TOKEN"],
            [
                (os.environ["TAU_S2S_LLM_BASE_URL"], os.environ["TAU_S2S_LLM_API_KEY"]),
                (
                    os.environ["TAU_S2S_LLM_FALLBACK_BASE_URL"],
                    os.environ["TAU_S2S_LLM_FALLBACK_API_KEY"],
                ),
            ],
        )
    audio = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=8766, log_level="warning", access_log=False
        )
    )
    thread = threading.Thread(target=audio.run, daemon=True)
    thread.start()
    runtime = None
    try:
        deadline = time.monotonic() + 15
        while not audio.started:
            if not thread.is_alive() or time.monotonic() > deadline or stop.wait(0.05):
                raise RuntimeError("Local MiMo audio service did not start")
        document = ConfigDocument(
            Path("/opt/tau-bridge/speech/pipeline.yaml"),
            pipeline_document(settings, os.environ),
        )
        validate_config(document)
        resolved = resolve_config(document, environ=os.environ)
        runtime = build_configured_server(resolved, stop)
        runtime.start()
        deadline = time.monotonic() + 15
        # The native /v1/pool endpoint indicates construction and all warmups ended.
        with httpx.Client(timeout=2, trust_env=False) as client:
            while not stop.wait(0.05):
                if (
                    not thread.is_alive()
                    or runtime.state == "failed"
                    or time.monotonic() > deadline
                ):
                    raise RuntimeError("Speech-to-Speech worker failed")
                if runtime.server and not runtime.server.stop_event.is_set():
                    try:
                        reply = client.get("http://127.0.0.1:8765/v1/pool")
                        if reply.is_success:
                            print("TAU_S2S_READY", flush=True)
                            break
                    except httpx.HTTPError:
                        pass
        runtime.wait()
    finally:
        stop.set()
        if runtime:
            runtime.stop()
        audio.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Native exceptions may embed request bodies or credentials; retain type only.
        print(
            f"Speech-to-Speech startup/runtime failed ({type(error).__name__})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from None

"""Compatibility import; implementation lives in tau2.adapters.qwen_audio_agent.transcription.qwen_streaming."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module(
    "tau2.adapters.qwen_audio_agent.transcription.qwen_streaming"
)

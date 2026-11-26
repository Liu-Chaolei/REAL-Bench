"""Serializable settings for the complete Qwen Audio Agent system."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tau2.config import (
    DEFAULT_ASR_RECHECK_BUFFER_SECONDS,
    DEFAULT_ASR_RECHECK_CONTEXT_SECONDS,
    DEFAULT_ASR_RECHECK_MAX_SECONDS,
    DEFAULT_ASR_RECHECK_TIMEOUT_SECONDS,
    DEFAULT_MIMO_API_BASE,
    DEFAULT_MIMO_API_KEY_ENV,
    DEFAULT_MIMO_ASR_MODEL,
    DEFAULT_MIMO_TTS_MODEL,
    DEFAULT_MIMO_TTS_VOICE,
    DEFAULT_PCM_SAMPLE_RATE,
    DEFAULT_QWEN_AGENT_ASR_MODEL,
    DEFAULT_QWEN_AGENT_BOOT_TIMEOUT_SECONDS,
    DEFAULT_QWEN_AGENT_IMAGE,
    DEFAULT_QWEN_AGENT_MAX_BUFFER_SECONDS,
    DEFAULT_QWEN_AGENT_TOOL_TIMEOUT_SECONDS,
    DEFAULT_QWEN_ASR_APPEND_SECONDS,
    DEFAULT_QWEN_ASR_STREAMING_TIMEOUT_SECONDS,
    DEFAULT_QWEN_OUTPUT_SAMPLE_RATE,
    DEFAULT_QWEN_TTS_API_BASE,
    DEFAULT_QWEN_TTS_API_KEY_ENV,
)
from tau2.data_model.asr import HotwordSettings
from tau2.data_model.audio import AudioEncoding, AudioFormat
from tau2.data_model.user_simulation import UserSimulationConfig
from tau2.utils.env_config import read_config_environment, resolve_env_reference

LEGACY_EVALUATION_FIELDS = {
    "tts",
    "user_decision_llm",
    "user_decision_llm_args",
    "user_llm_api_key_env",
    "user_llm_base_url_env",
    "user_llm_model_env",
    "dynamic_followup",
    "user_silence_checkin_seconds",
    "watchdog",
}

# Qwen receives the PCM stream produced by the SAASR caller renderer. The
# adapter converts this external contract to the provider's negotiated rate.
QWEN_AGENT_INPUT_AUDIO_FORMAT = AudioFormat(
    encoding=AudioEncoding.PCM_S16LE,
    sample_rate=DEFAULT_PCM_SAMPLE_RATE,
    channels=1,
)


class SpeechToSpeechConfig(BaseModel):
    """Managed native speech frontend; credentials remain dotenv references."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    llm_api_key_env: str = "LLM_API_KEY"
    llm_base_url_env: str = "LLM_API_URL"
    llm_model_env: str = "LLM_MODEL"
    llm_fallback_api_key_env: str | None = None
    llm_fallback_base_url_env: str | None = None
    mimo_api_key_env: str = DEFAULT_MIMO_API_KEY_ENV
    mimo_base_url: str = DEFAULT_MIMO_API_BASE
    asr_model: Literal["mimo-v2.5-asr", "qwen-audio-3.1-asr-flash"] = (
        DEFAULT_MIMO_ASR_MODEL
    )
    asr_provider_env: str = "QWEN_AUDIO_REALTIME_PROVIDER"
    asr_base_url_env: str = "QWEN_AUDIO_REALTIME_BASE_URL"
    asr_api_key_env: str = "DASHSCOPE_API_KEY3"
    tts_model: Literal["mimo-v2.5-tts"] = DEFAULT_MIMO_TTS_MODEL
    voice: Literal["Dean", "Chloe", "Mia", "Milo"] = DEFAULT_MIMO_TTS_VOICE
    language: str = "en"
    llm_api: Literal["responses-api"] = "responses-api"
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    request_timeout_seconds: float = Field(default=60, gt=0)

    @field_validator(
        "llm_api_key_env",
        "llm_base_url_env",
        "llm_model_env",
        "mimo_api_key_env",
        "asr_provider_env",
        "asr_base_url_env",
        "asr_api_key_env",
        "llm_fallback_api_key_env",
        "llm_fallback_base_url_env",
    )
    @classmethod
    def references(cls, value):
        """Accept environment variable names only."""
        if value is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError(
                "Speech settings must reference environment variable names"
            )
        return value

    @model_validator(mode="after")
    def paired_fallback(self):
        """A fallback endpoint needs both its own URL and credential reference."""
        if (self.llm_fallback_api_key_env is None) != (
            self.llm_fallback_base_url_env is None
        ):
            raise ValueError(
                "Speech LLM fallback requires both key and base URL references"
            )
        return self


class QwenAgentConfig(BaseModel):
    """Launch one isolated system per call; credentials are environment references."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    image: str = DEFAULT_QWEN_AGENT_IMAGE
    domain: str | None = Field(default=None, exclude=True)
    api_configuration_resolved: bool = False
    api_profiles: dict = Field(default_factory=dict)
    api_config_dir: str | None = None
    config_env_file: str | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_shared_api_settings(cls, value):
        """Normalize historical caller fields once, then resolve observer settings."""
        if not isinstance(value, dict):
            return value
        from tau2.data_model.user_simulation import merge_user_settings

        value = dict(value)
        legacy = {
            key: value.pop(key) for key in LEGACY_EVALUATION_FIELDS if key in value
        }
        context = {
            key: value[key]
            for key in ("api_config_dir", "config_env_file")
            if value.get(key)
        }
        supplied = value.get("evaluation")
        if supplied is not None or legacy:
            value["evaluation"] = merge_user_settings(supplied, legacy, context=context)
        elif "evaluation" not in value:
            value["evaluation"] = UserSimulationConfig(**context)
        if not value.get("api_config_dir") or value.get("api_configuration_resolved"):
            return value
        from pathlib import Path

        from tau2.utils.api_config import get_api_configuration

        directory = str(Path(value["api_config_dir"]).expanduser().resolve())
        api = get_api_configuration(directory)
        if value.get("environment"):
            raise ValueError(
                "api_config_dir conflicts with explicit Agent environment mappings"
            )
        value["api_config_dir"] = directory
        value["api_configuration_resolved"] = True
        value["config_env_file"] = str(Path(directory) / "realbench.env")
        for suffix, target in (
            ("PROVIDER", "asr_provider"),
            ("MODEL", "asr_model"),
            ("BASE_URL", "asr_base_url"),
        ):
            setting = api.value("REALBENCH_ASR_" + suffix)
            if not setting:
                raise ValueError("Missing REALBENCH_ASR_" + suffix)
            value[target] = setting
        if not api.value("REALBENCH_ASR_API_KEY"):
            raise ValueError("Missing environment variable REALBENCH_ASR_API_KEY")
        value.update(
            asr_api_key_env="REALBENCH_ASR_API_KEY",
            asr_base_url_env=None,
            asr_provider_env=None,
        )
        return value

    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Container variable -> host variable name, never credential values.",
    )
    boot_timeout_seconds: float = Field(
        default=DEFAULT_QWEN_AGENT_BOOT_TIMEOUT_SECONDS, gt=0
    )
    tool_timeout_seconds: float = Field(
        default=DEFAULT_QWEN_AGENT_TOOL_TIMEOUT_SECONDS, gt=0
    )
    max_buffer_seconds: float = Field(
        default=DEFAULT_QWEN_AGENT_MAX_BUFFER_SECONDS, gt=0
    )
    speech_to_speech: SpeechToSpeechConfig | None = None
    evaluation: UserSimulationConfig | None = None

    def __getattr__(self, name):
        # Old callers may inspect the historical config. Runtime code reads the
        # resolved AudioNativeConfig.user_simulation instead.
        if name in LEGACY_EVALUATION_FIELDS:
            evaluation = object.__getattribute__(self, "__dict__").get("evaluation")
            if evaluation is not None:
                return getattr(evaluation, name)
        return super().__getattr__(name)

    def user_llm_settings(self):
        """Compatibility for historical standalone integration launchers."""
        return self.evaluation.user_llm_settings() if self.evaluation else None

    asr_provider: Literal["deepgram", "mimo", "qwen"] = "qwen"
    asr_base_url: str = DEFAULT_QWEN_TTS_API_BASE
    asr_base_url_env: str | None = None
    asr_provider_env: str | None = None
    asr_hotwords: HotwordSettings = Field(default_factory=HotwordSettings)
    asr_segment_seconds: float = Field(default=4.0, ge=1, le=15)
    asr_append_seconds: float = Field(
        default=DEFAULT_QWEN_ASR_APPEND_SECONDS, ge=0.2, le=2
    )
    playback_sample_rate: Literal[16000, 24000] = DEFAULT_QWEN_OUTPUT_SAMPLE_RATE
    asr_streaming_timeout_seconds: float = Field(
        default=DEFAULT_QWEN_ASR_STREAMING_TIMEOUT_SECONDS, gt=0
    )
    asr_silence_seconds: float = Field(default=0.6, ge=0.2, le=3)
    asr_speech_rms: int = Field(default=80, ge=1, le=32767)
    asr_recheck_enabled: bool = True
    asr_recheck_buffer_seconds: float = Field(
        default=DEFAULT_ASR_RECHECK_BUFFER_SECONDS, ge=1
    )
    asr_recheck_max_seconds: float = Field(
        default=DEFAULT_ASR_RECHECK_MAX_SECONDS, ge=1, le=20
    )
    asr_recheck_context_seconds: float = Field(
        default=DEFAULT_ASR_RECHECK_CONTEXT_SECONDS, ge=0, le=2
    )
    asr_recheck_timeout_seconds: float = Field(
        default=DEFAULT_ASR_RECHECK_TIMEOUT_SECONDS, gt=0, le=15
    )
    asr_model: str = DEFAULT_QWEN_AGENT_ASR_MODEL
    asr_api_key_env: str = DEFAULT_QWEN_TTS_API_KEY_ENV

    @field_validator("environment")
    @classmethod
    def environment_references(cls, value):
        """Accept variable names, not values or overrides of the harness contract."""
        for target, source in value.items():
            if not all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", s) for s in (target, source)):
                raise ValueError("environment must map environment variable names")
            if target.startswith(("TAU_", "QWAUDIO_", "QWEN_CODE_TAU_")) or target in {
                "HOME",
                "QWEN_HOME",
                "QWEN_RUNTIME_DIR",
                "LD_PRELOAD",
                "LD_LIBRARY_PATH",
                "PATH",
                "TMPDIR",
                "NODE_OPTIONS",
                "NODE_PATH",
                "AGENT_PROTOCOL",
                "QWEN_CODE_BIN",
                "QWEN_CODE_WORKSPACE",
                "QWEN_AUDIO_AGENT_FRONTEND_PROMPT_DIR",
                "QWEN_AUDIO_FRONTEND_MCP_CONFIG",
                "QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE",
            }:
                raise ValueError(f"{target} is controlled by the evaluation harness")
            if target not in {
                "QWEN_AUDIO_REALTIME_API_KEY",
                "QWEN_AUDIO_REALTIME_URL",
                "QWEN_AUDIO_REALTIME_PROVIDER",
                "QWEN_AUDIO_REALTIME_MODEL",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_MODEL",
                "QWEN_CODE_REASONING_EFFORT",
            }:
                raise ValueError(f"{target} is not an Agent connection setting")
        return value

    def resolved_environment(self, ambient: dict[str, str]) -> dict[str, str]:
        """Read dotenv without executing it; resolve only selected Agent settings.

        Ambient settings override the file, including referenced variables.
        Neither values nor file contents belong in serialized configuration.
        """
        if self.api_config_dir and not self.speech_to_speech:
            from tau2.utils.api_config import get_api_configuration

            return get_api_configuration(self.api_config_dir, "qw").agent_environment()
        if self.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            values = get_api_configuration(self.api_config_dir, "qw").values
            ambient = {}
        else:
            values = read_config_environment(self.config_env_file, ambient)

        def resolve(name):
            return resolve_env_reference(values, name)

        aliases = {
            "QWEN_AUDIO_REALTIME_API_KEY": (
                "QWEN_AUDIO_REALTIME_API_KEY",
                "DASHSCOPE_API_KEY",
            ),
            "QWEN_AUDIO_REALTIME_URL": (
                "QWEN_AUDIO_REALTIME_URL",
                "QWEN_AUDIO_REALTIME_BASE_URL",
            ),
            "QWEN_AUDIO_REALTIME_PROVIDER": ("QWEN_AUDIO_REALTIME_PROVIDER",),
            "QWEN_AUDIO_REALTIME_MODEL": ("QWEN_AUDIO_REALTIME_MODEL",),
            "OPENAI_API_KEY": (
                "QWEN_CODE_API_KEY",
                "GAIA2_BACKEND_API_KEY",
                "OPENAI_API_KEY",
            ),
            "OPENAI_BASE_URL": (
                "QWEN_CODE_API_BASE",
                "GAIA2_BACKEND_API_BASE",
                "OPENAI_BASE_URL",
            ),
            "OPENAI_MODEL": ("QWEN_CODE_MODEL", "GAIA2_BACKEND_MODEL", "OPENAI_MODEL"),
            "QWEN_CODE_REASONING_EFFORT": (
                "QWEN_CODE_REASONING_EFFORT",
                "GAIA2_BACKEND_REASONING_EFFORT",
            ),
        }
        selected = {}
        for target, candidates in aliases.items():
            if self.speech_to_speech and target.startswith("QWEN_AUDIO_REALTIME_"):
                continue
            source = self.environment.get(target)
            if source is None:
                source = next((key for key in candidates if ambient.get(key)), None)
            if source is None:
                source = next((key for key in candidates if values.get(key)), None)
            if source:
                selected[target] = resolve(source)
        for target, source in self.environment.items():
            if self.speech_to_speech and target.startswith("QWEN_AUDIO_REALTIME_"):
                continue
            selected[target] = resolve(source)
        if self.speech_to_speech:
            speech = self.speech_to_speech
            for target, source in {
                "TAU_S2S_LLM_API_KEY": speech.llm_api_key_env,
                "TAU_S2S_LLM_BASE_URL": speech.llm_base_url_env,
                "TAU_S2S_LLM_MODEL": speech.llm_model_env,
                "TAU_S2S_MIMO_API_KEY": speech.mimo_api_key_env,
            }.items():
                selected[target] = resolve(source)
                if not selected[target]:
                    raise ValueError(f"Missing speech setting {source}")
            if speech.asr_model == "qwen-audio-3.1-asr-flash":
                if resolve(speech.asr_provider_env) != "dashscope":
                    raise ValueError(
                        "Qwen frontend ASR requires the dashscope provider"
                    )
                for target, source in {
                    "TAU_S2S_ASR_API_KEY": speech.asr_api_key_env,
                    "TAU_S2S_ASR_BASE_URL": speech.asr_base_url_env,
                }.items():
                    selected[target] = resolve(source)
                    if not selected[target]:
                        raise ValueError(f"Missing speech setting {source}")
            if speech.llm_fallback_api_key_env is not None:
                for target, source in {
                    "TAU_S2S_LLM_FALLBACK_API_KEY": speech.llm_fallback_api_key_env,
                    "TAU_S2S_LLM_FALLBACK_BASE_URL": speech.llm_fallback_base_url_env,
                }.items():
                    selected[target] = resolve(source)
                    if not selected[target]:
                        raise ValueError(f"Missing speech setting {source}")
            selected.update(
                TAU_S2S_CONFIG=speech.model_dump_json(),
                QWEN_AUDIO_REALTIME_PROVIDER="speech-to-speech",
                QWEN_AUDIO_REALTIME_URL="ws://127.0.0.1:8765/v1/realtime",
                QWEN_AUDIO_REALTIME_API_KEY="",
                QWEN_AUDIO_REALTIME_MODEL="",
                QWEN_AUDIO_REALTIME_VOICE="",
            )
        required = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"]
        if not self.speech_to_speech:
            if selected.get("QWEN_AUDIO_REALTIME_PROVIDER") in {
                "speech-to-speech",
                "s2s",
            }:
                required.append("QWEN_AUDIO_REALTIME_URL")
            else:
                required.append("QWEN_AUDIO_REALTIME_API_KEY")
        for target in required:
            if not selected.get(target):
                raise ValueError(f"Missing Agent setting {target}")
        effort = selected.get("QWEN_CODE_REASONING_EFFORT")
        if effort and effort not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise ValueError("Invalid backend reasoning effort")
        selected["OPENAI_MODEL"] = selected["OPENAI_MODEL"].removeprefix("openai/")
        return selected

    @model_validator(mode="after")
    def observer_profile(self):
        """Reject inconsistent observer profiles before any model startup."""
        from tau2.config import DEFAULT_QWEN_ASR_MODEL, DEFAULT_QWEN_ASR_STREAMING_MODEL

        if self.asr_hotwords.enabled and self.asr_provider != "qwen":
            raise ValueError("Automatic hotwords require the Qwen user-side ASR")
        if self.asr_provider == "qwen":
            if self.asr_model not in {
                DEFAULT_QWEN_ASR_MODEL,
                DEFAULT_QWEN_ASR_STREAMING_MODEL,
            }:
                raise ValueError("Qwen user-side ASR requires a Qwen 3.1 ASR model")
            if (
                self.asr_base_url_env is None
                and self.asr_base_url == DEFAULT_MIMO_API_BASE
            ):
                raise ValueError("Qwen user-side ASR requires its own base URL")
        return self

    @field_validator("asr_api_key_env", "asr_base_url_env", "asr_provider_env")
    @classmethod
    def credential_reference(cls, value):
        """Keep secrets out of saved configuration."""
        if value is not None and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
            raise ValueError("asr_api_key_env must be an environment variable name")
        return value

    @field_validator("image")
    @classmethod
    def image_reference(cls, value):
        """Reject Docker option strings; subprocesses never invoke a shell."""
        if not value or value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("image must be a Docker image reference")
        return value

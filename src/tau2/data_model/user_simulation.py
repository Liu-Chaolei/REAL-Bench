"""Evaluator-owned caller and runtime controls, independent of the tested Agent."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from tau2.config import DEFAULT_USER_SILENCE_CHECKIN_SECONDS
from tau2.data_model.dynamic_followup import DynamicFollowupConfig
from tau2.data_model.voice import MiMoTTSConfig, QwenTTSConfig
from tau2.data_model.watchdog import WatchdogConfig
from tau2.utils.env_config import read_config_environment, resolve_env_reference


class UserSimulationConfig(BaseModel):
    """Caller model/voice, followup policy and watchdog settings owned by evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    api_config_dir: str | None = None
    config_env_file: str | None = None
    api_configuration_resolved: bool = False
    api_profiles: dict = Field(default_factory=dict)
    tts: MiMoTTSConfig | QwenTTSConfig | None = None
    user_decision_llm: str | None = None
    user_decision_llm_args: dict = Field(default_factory=dict)

    @field_serializer("user_decision_llm_args", when_used="json")
    def public_decision_arguments(self, value):
        """Resolved credentials stay in memory rather than saved run settings."""
        return {
            key: item
            for key, item in value.items()
            if key not in {"api_key", "token", "authorization"}
        }

    user_llm_api_key_env: str | None = None
    user_llm_base_url_env: str | None = None
    user_llm_model_env: str | None = None
    dynamic_followup: DynamicFollowupConfig = Field(
        default_factory=DynamicFollowupConfig,
        description=(
            "Opt-in contextual rendering for the second and third fixed scenario requests"
        ),
    )

    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    @model_validator(mode="before")
    @classmethod
    def resolve_shared_api_settings(cls, value):
        """Resolve caller roles and TTS at configuration loading, without secrets."""
        if (
            not isinstance(value, dict)
            or not value.get("api_config_dir")
            or value.get("api_configuration_resolved")
        ):
            return value
        from pathlib import Path

        from tau2.utils.api_config import get_api_configuration

        value = dict(value)
        directory = str(Path(value["api_config_dir"]).expanduser().resolve())
        api = get_api_configuration(directory)
        value["api_config_dir"] = directory
        value["api_configuration_resolved"] = True
        roles = ["USER", "USER_DECISION"]
        followup = value.get("dynamic_followup") or {}
        if followup.get("enabled") if isinstance(followup, dict) else followup.enabled:
            roles.append("FOLLOWUP")
        value["api_profiles"] = {role: api.text(role).public() for role in roles}
        if any(
            value.get(k)
            for k in (
                "user_llm_api_key_env",
                "user_llm_base_url_env",
                "user_llm_model_env",
                "user_decision_llm",
            )
        ):
            raise ValueError(
                "api_config_dir conflicts with explicit caller connection settings"
            )
        value["config_env_file"] = str(Path(directory) / "realbench.env")
        if not api.value("REALBENCH_TTS_API_KEY"):
            raise ValueError("Missing environment variable REALBENCH_TTS_API_KEY")
        provider = api.value("REALBENCH_TTS_PROVIDER")
        if provider not in {"qwen", "mimo"}:
            raise ValueError("REALBENCH_TTS_PROVIDER must be qwen or mimo")
        settings = dict(value.get("tts") or {})
        for suffix, target in (
            ("MODEL", "model_id"),
            ("BASE_URL", "base_url"),
            ("VOICE", "voice_id"),
        ):
            setting = api.value("REALBENCH_TTS_" + suffix)
            if not setting:
                raise ValueError("Missing REALBENCH_TTS_" + suffix)
            settings[target] = setting
        settings.update(
            api_key_env="REALBENCH_TTS_API_KEY",
            config_env_file=value["config_env_file"],
            voices=api.json_value("REALBENCH_TTS_VOICES_JSON", {}),
        )
        value["tts"] = (
            QwenTTSConfig if provider == "qwen" else MiMoTTSConfig
        ).model_validate(settings)
        return value

    @model_validator(mode="after")
    def paired_user_llm_references(self):
        """Accept a complete caller profile containing variable names only."""
        references = (
            self.user_llm_api_key_env,
            self.user_llm_base_url_env,
            self.user_llm_model_env,
        )
        if any(references) and not all(references):
            raise ValueError(
                "Caller LLM configuration requires key, base URL and model references"
            )
        for reference in references:
            if reference is not None and not re.fullmatch(
                r"[A-Z_][A-Z0-9_]*", reference
            ):
                raise ValueError(
                    "Caller settings must reference environment variable names"
                )
        return self

    def user_llm_settings(self) -> tuple[str, dict] | None:
        """Resolve an explicitly configured caller LLM without serializing secrets."""
        if self.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            profile = get_api_configuration(self.api_config_dir).text("USER")
            return profile.litellm_model, profile.arguments()
        references = (
            self.user_llm_api_key_env,
            self.user_llm_base_url_env,
            self.user_llm_model_env,
        )
        if not any(references):
            return None
        if not all(references):
            raise ValueError(
                "Caller LLM configuration requires key, base URL and model references"
            )
        values = read_config_environment(self.config_env_file)
        model = resolve_env_reference(values, self.user_llm_model_env)
        return (
            model if model.startswith("openai/") else "openai/" + model,
            {
                "api_key": resolve_env_reference(values, self.user_llm_api_key_env),
                "api_base": resolve_env_reference(values, self.user_llm_base_url_env),
                "num_retries": 0,
            },
        )

    user_silence_checkin_seconds: float = Field(
        default=DEFAULT_USER_SILENCE_CHECKIN_SECONDS,
        ge=0,
        allow_inf_nan=False,
        description="Minimum silence before an unsolicited user check-in; "
        "fresh agent speech and normal replies retain their existing timing. "
        "Disabled by default; set to a positive value to enable it.",
    )


def merge_user_settings(explicit, legacy, *, context=None):
    """Normalize one legacy input and reject conflicting explicit overrides."""
    if not legacy and explicit is not None:
        if isinstance(explicit, UserSimulationConfig):
            return explicit
        return UserSimulationConfig.model_validate({**(context or {}), **explicit})
    legacy_value = (
        legacy.model_dump(mode="python")
        if isinstance(legacy, UserSimulationConfig)
        else dict(legacy or {})
    )
    old = UserSimulationConfig.model_validate({**(context or {}), **legacy_value})
    if explicit is None:
        return old
    new = UserSimulationConfig.model_validate(explicit)
    old_fields = (
        legacy.model_fields_set
        if isinstance(legacy, UserSimulationConfig)
        else set(legacy_value)
    )
    for key in old_fields & new.model_fields_set:
        if getattr(old, key) != getattr(new, key):
            raise ValueError(
                f"Legacy Qwen caller setting conflicts with user_simulation.{key}"
            )
    values = old.model_dump(mode="python")
    values.update({key: getattr(new, key) for key in new.model_fields_set})
    return UserSimulationConfig.model_validate(values)

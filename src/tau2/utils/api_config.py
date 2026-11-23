"""Private API connection snapshots shared by agent, scoring and authoring."""

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from tau2.config import DEFAULT_API_CONFIG_DIR
from tau2.utils.env_config import read_environment_files, resolve_env_reference

ROLE_PARENTS = {
    **dict.fromkeys(
        (
            "USER_DECISION",
            "USER_GROUNDING",
            "FOLLOWUP",
            "HIDDEN_INTENT",
            "ASR_HOTWORDS",
            "ACTIVE_REQUEST",
        ),
        "USER",
    ),
    **dict.fromkeys(
        (
            "INTENT_GT",
            "INTENT_PREDICTION",
            "INTENT_EVALUATION",
            "EXECUTION",
            "INTERACTION",
            "STATE_INTERACTION",
            "FEEDBACK_NUGGETS",
            "FEEDBACK_ALIGNMENT",
            "REPLY_JUDGE",
        ),
        "JUDGE",
    ),
}
DIAGNOSTIC_ROLES = {
    "gt": "INTENT_GT",
    "prediction": "INTENT_PREDICTION",
    "evaluation": "INTENT_EVALUATION",
    **{
        x: x.upper()
        for x in (
            "execution",
            "interaction",
            "state_interaction",
            "feedback_nuggets",
            "feedback_alignment",
        )
    },
}


def safe_base_url(url: str) -> str:
    """Strip authentication and query parameters from recorded endpoints."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
    )


class TextApiProfile(BaseModel):
    """Resolved connection with credentials excluded from dumps and repr."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    role: str
    inherited_from: str | None = None
    model: str
    base_url: str = Field(exclude=True, repr=False)
    api: str
    api_key: SecretStr = Field(exclude=True, repr=False)
    key_reference: str
    args: dict = Field(default_factory=dict)
    source: str

    @property
    def litellm_model(self) -> str:
        """Return one canonical provider/protocol prefix."""
        model = self.model.removeprefix("openai/responses/").removeprefix("openai/")
        return ("openai/responses/" if self.api == "responses" else "openai/") + model

    def arguments(self) -> dict:
        """Build ephemeral client arguments; callers must not serialize them."""
        return dict(
            self.args, api_base=self.base_url, api_key=self.api_key.get_secret_value()
        )

    def public(self) -> dict:
        """Non-secret identity for provenance and cache keys."""
        return dict(
            role=self.role,
            inherited_from=self.inherited_from,
            model=self.litellm_model,
            api=self.api,
            api_base=safe_base_url(self.base_url),
            args=self.args,
            key_reference=self.key_reference,
            source=self.source,
        )


class ApiConfiguration:
    """One file snapshot; no ambient environment or per-request file reads."""

    def __init__(self, directory, kind="realbench"):
        self.directory = Path(directory).expanduser().resolve()
        self.kind = kind
        self.values = read_environment_files(
            [self.directory / "config.env", self.directory / f"{kind}.env"]
        )
        self._profiles = {}

    def value(self, name, default=None):
        """Resolve a selected setting, preserving safe variable-only errors."""
        if name not in self.values:
            return default
        return resolve_env_reference(self.values, name)

    def text(self, role) -> TextApiProfile:
        """Resolve a role with fixed inheritance and complete connection overrides."""
        role = role.upper()
        if role in self._profiles:
            return self._profiles[role]
        prefix = "REALBENCH_" + role + "_"
        parent = ROLE_PARENTS.get(role)
        connection = ("MODEL", "BASE_URL", "API_KEY", "API")
        present = [prefix + x in self.values for x in connection]
        inherited = None
        if any(present):
            if not all(present):
                raise ValueError(
                    f"Incomplete API connection for {role}: set MODEL, BASE_URL, API_KEY and API"
                )
            resolved = {x: self.value(prefix + x) for x in connection}
            if any(not x for x in resolved.values()):
                raise ValueError(f"Empty API connection setting for {role}")
            if resolved["API"] not in {"chat", "responses"}:
                raise ValueError(f"Unsupported API protocol for {role}")
            refs = re.findall(
                r"\$\{([A-Z_][A-Z0-9_]*)\}", self.values[prefix + "API_KEY"]
            )
            profile = TextApiProfile(
                role=role,
                model=resolved["MODEL"],
                base_url=resolved["BASE_URL"],
                api_key=SecretStr(resolved["API_KEY"]),
                api=resolved["API"],
                key_reference=refs[0] if refs else prefix + "API_KEY",
                source=str(self.directory / "realbench.env"),
            )
        elif parent:
            profile = self.text(parent).model_copy(
                update={
                    "role": role,
                    "inherited_from": parent,
                    "args": dict(self.text(parent).args),
                }
            )
            inherited = parent
        else:
            raise ValueError(f"Missing API connection for {role}")
        args = dict(profile.args)
        for suffix, name, convert in (
            ("REASONING_EFFORT", "reasoning_effort", str),
            ("TEMPERATURE", "temperature", float),
            ("MAX_TOKENS", "max_tokens", int),
            ("NUM_RETRIES", "num_retries", int),
            ("TIMEOUT_SECONDS", "timeout", float),
        ):
            value = self.value(prefix + suffix)
            if value:
                try:
                    args[name] = convert(value)
                except ValueError:
                    raise ValueError(f"Invalid parameter {prefix + suffix}") from None
        profile = profile.model_copy(update={"args": args, "inherited_from": inherited})
        self._profiles[role] = profile
        return profile

    def fallback(self, role):
        """Return only an explicitly configured complete fallback connection."""
        fallback_role = role.upper() + "_FALLBACK"
        prefix = "REALBENCH_" + fallback_role + "_"
        return (
            self.text(fallback_role)
            if any(k.startswith(prefix) for k in self.values)
            else None
        )

    def agent_environment(self):
        """Translate qw.env roles into the existing Qwen container contract."""
        mapping = {
            "OPENAI_MODEL": "GAIA2_BACKEND_MODEL",
            "OPENAI_BASE_URL": "GAIA2_BACKEND_API_BASE",
            "OPENAI_API_KEY": "GAIA2_BACKEND_API_KEY",
            "QWEN_CODE_REASONING_EFFORT": "GAIA2_BACKEND_REASONING_EFFORT",
            "QWEN_AUDIO_REALTIME_PROVIDER": "QWEN_AUDIO_REALTIME_PROVIDER",
            "QWEN_AUDIO_REALTIME_MODEL": "QWEN_AUDIO_REALTIME_MODEL",
            "QWEN_AUDIO_REALTIME_URL": "QWEN_AUDIO_REALTIME_BASE_URL",
            "QWEN_AUDIO_REALTIME_API_KEY": "DASHSCOPE_API_KEY",
        }
        result = {
            target: self.value(source)
            for target, source in mapping.items()
            if source in self.values
        }
        for target in (
            "OPENAI_MODEL",
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "QWEN_AUDIO_REALTIME_PROVIDER",
            "QWEN_AUDIO_REALTIME_MODEL",
            "QWEN_AUDIO_REALTIME_URL",
            "QWEN_AUDIO_REALTIME_API_KEY",
        ):
            if not result.get(target):
                raise ValueError(f"Missing Agent setting {mapping[target]}")
        return result

    def json_value(self, name, default=None):
        """Expand JSON string leaves safely even when references contain quotes."""
        raw = self.values.get(name)
        if raw is None:
            return default
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ValueError(f"Invalid JSON in {name}") from None

        def expand(item):
            if isinstance(item, str):
                return re.sub(
                    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                    lambda m: self.value(m[1])
                    if m[1] in self.values
                    else resolve_env_reference(self.values, m[1]),
                    item,
                )
            if isinstance(item, list):
                return [expand(x) for x in item]
            if isinstance(item, dict):
                return {k: expand(v) for k, v in item.items()}
            return item

        return expand(parsed)


@lru_cache(maxsize=32)
def _configuration_snapshot(directory: str, kind: str):
    return ApiConfiguration(directory, kind)


def get_api_configuration(directory=DEFAULT_API_CONFIG_DIR, kind="realbench"):
    """Reuse a canonical startup snapshot within a process."""
    return _configuration_snapshot(str(Path(directory).expanduser().resolve()), kind)

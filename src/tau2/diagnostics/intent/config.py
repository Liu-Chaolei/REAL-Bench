import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

STAGE_ENV_PREFIXES = {
    "gt": "INTENT_GT",
    "prediction": "INTENT_PREDICTION",
    "evaluation": "INTENT_EVALUATION",
}
MODEL_ENV_KEYS = frozenset(
    f"{prefix}_{suffix}"
    for prefix in STAGE_ENV_PREFIXES.values()
    for suffix in ("MODEL", "API_BASE", "API_KEY")
)


class ModelConfigurationError(ValueError):
    """Safe diagnostic: only stage and variable names, never credential values."""

    def __init__(self, stage: str, missing_variables: list[str]):
        self.diagnostic = {"stage": stage, "missing_variables": missing_variables}
        super().__init__(
            f"{stage}: set {', '.join(missing_variables)} in .env or the environment"
        )


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelOptions(StrictConfig):
    """Serializable stage options. Model identity and credentials are environment-only."""

    temperature: float | None = 0
    reasoning_effort: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    response_format: Literal["json_schema", "json_object"] = "json_schema"
    timeout_seconds: float = Field(default=120, gt=0)


class ModelConfig(ModelOptions):
    """Resolved runtime settings used by clients, provenance and low-level stages."""

    model: str | None = None
    api_base: str | None = None
    api_key_env: str | None = None
    stage: str | None = None


class SourceConfig(StrictConfig):
    path: Path
    format: Literal["jsonl", "json", "csv", "txt", "txt_dir", "gaia2", "backend"] = (
        "jsonl"
    )
    gateway_path: Path | None = None
    scope: Literal["all_submissions", "initial_request"] = "all_submissions"
    observation_path: Path | None = None
    id_field: str = "id"
    text_field: str = "text"
    context_field: str | None = "context"
    metadata_field: str | None = "metadata"
    record_id: str | None = None
    configs: list[str] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)
    start: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_backend_source(self) -> "SourceConfig":
        if self.format == "backend" and (
            not self.record_id or self.gateway_path is None
        ):
            raise ValueError("Backend inputs require record_id and gateway_path")
        if (
            self.format == "backend"
            and self.scope == "initial_request"
            and self.observation_path is None
        ):
            raise ValueError("Initial-request backend inputs require observation_path")
        return self


class ScoringProfile(StrictConfig):
    name: str = "generic-leaves"
    strategy: Literal["generic", "gaia2"] = "generic"
    ignored_kind_values: list[str] = Field(default_factory=list)
    merge_rules: list[str] = Field(default_factory=list)
    ignored_paths: list[str] = Field(default_factory=list)
    ignored_keys: list[str] = Field(default_factory=list)
    atomic_paths: list[str] = Field(default_factory=list)
    atomic_kinds: list[str] = Field(default_factory=list)
    atomic_collection_keys: list[str] = Field(default_factory=list)
    definition_key: str | None = None
    reference_keys: list[str] = Field(default_factory=list)
    local_definition_keys: list[str] = Field(default_factory=list)
    local_reference_keys: list[str] = Field(default_factory=list)
    local_reference_kinds: list[str] = Field(default_factory=list)
    component_map: dict[str, str] = Field(default_factory=dict)
    # Explicit groups, each describing different representations of one fact.
    merge_groups: list[list[str]] = Field(default_factory=list)
    # Optional frozen item inventory for reproducing an existing annotation policy.
    inventory: list[dict[str, Any]] | None = None


class NodeDefinition(StrictConfig):
    namespace: str
    path: str
    key: str = "id"
    owner_namespace: str | None = None
    branch: str | None = None


class ReferenceDefinition(StrictConfig):
    key: str
    namespace: str
    role: str
    kind: str | None = None
    local: bool = False


class ValidationProfile(StrictConfig):
    name: str = "generic-structure"
    nodes: list[NodeDefinition] = Field(default_factory=list)
    references: list[ReferenceDefinition] = Field(default_factory=list)
    global_unique_ids: bool = False
    ambiguity_paths: bool = False
    inline_selections: bool = False
    value_rules: Literal["none", "gaia2"] = "none"


class HookConfig(StrictConfig):
    gt_path: Path
    # Keys are source_id or config/source_id; values are normalized GT record IDs.
    request_map: Path
    timeout_seconds: float = Field(default=300, gt=0)


class IntentConfig(StrictConfig):
    schema_path: Path | None = None
    definition_set: str | None = None
    validation_profile_path: Path | None = None
    env_file: Path | None = None
    api_config_dir: str | None = None
    instructions_path: Path | None = None
    profile_path: Path | None = None
    gt_model: ModelOptions = Field(default_factory=ModelOptions)
    prediction_model: ModelOptions = Field(default_factory=ModelOptions)
    evaluation_model: ModelOptions = Field(default_factory=ModelOptions)
    gt_source: SourceConfig | None = None
    prediction_source: SourceConfig | None = None
    output_dir: Path = Path("intent-results")
    concurrency: int = Field(default=1, gt=0)
    report_group_fields: list[str] = Field(default_factory=list)
    resume: bool = True
    qwen: HookConfig | None = None

    @model_validator(mode="after")
    def validate_definition_source(self) -> "IntentConfig":
        custom = (
            self.schema_path,
            self.instructions_path,
            self.profile_path,
            self.validation_profile_path,
        )
        if self.definition_set and any(value is not None for value in custom):
            raise ValueError("Use definition_set or custom definition paths, not both")
        if not self.definition_set and self.schema_path is None:
            raise ValueError("Configure definition_set or schema_path")
        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            load_definition(self.definition_set)
        return self

    @classmethod
    def load(cls, path: Path) -> "IntentConfig":
        raw = yaml.safe_load(path.read_text())
        if isinstance(raw, dict):
            for stage in STAGE_ENV_PREFIXES:
                options = raw.get(f"{stage}_model")
                if isinstance(options, dict) and any(
                    key in options
                    for key in ("model", "api_base", "api_key_env", "api_key")
                ):
                    raise ValueError(
                        f"{stage}_model: move model, API base and key settings to .env; "
                        "YAML accepts only temperature, reasoning_effort, max_tokens, "
                        "response_format and timeout_seconds"
                    )
        config = cls.model_validate(raw)
        base = path.resolve().parent
        for key in (
            "schema_path",
            "validation_profile_path",
            "env_file",
            "instructions_path",
            "profile_path",
            "output_dir",
        ):
            value = getattr(config, key)
            if value is not None and not value.is_absolute():
                setattr(config, key, base / value)
        for source in (config.gt_source, config.prediction_source):
            if source is not None:
                for key in ("path", "gateway_path", "observation_path"):
                    value = getattr(source, key)
                    if value is not None and not value.is_absolute():
                        setattr(source, key, base / value)
        if config.qwen:
            for key in ("gt_path", "request_map"):
                value = getattr(config.qwen, key)
                if not value.is_absolute():
                    setattr(config.qwen, key, base / value)
        return config

    def environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Read into a private snapshot; never alter process/provider environments."""
        if self.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            api = get_api_configuration(self.api_config_dir)
            result = {}
            for stage, prefix in STAGE_ENV_PREFIXES.items():
                profile = api.text("INTENT_" + stage.upper())
                result.update(
                    {
                        prefix + "_MODEL": profile.litellm_model,
                        prefix + "_API_BASE": profile.base_url,
                        prefix + "_API_KEY": profile.api_key.get_secret_value(),
                    }
                )
            return result | (overrides or {})
        values = (
            dotenv_values(self.env_file)
            if self.env_file is not None and self.env_file.is_file()
            else {}
        )
        # Empty process variables (including untouched dotenv placeholders) are
        # unset; explicit API overrides can still intentionally clear a value.
        runtime = {key: value for key, value in os.environ.items() if value}
        merged = values | runtime | (overrides or {})

        def resolve(key: str, seen: set[str]) -> str:
            if key in seen:
                raise ValueError(f"Cyclic intent environment reference: {key}")
            value = merged.get(key) or ""
            if value in MODEL_ENV_KEYS:
                suffix = next(
                    suffix
                    for suffix in ("MODEL", "API_BASE", "API_KEY")
                    if key.endswith("_" + suffix)
                )
                if not value.endswith("_" + suffix):
                    raise ValueError(
                        f"Intent environment reference has a different role: {key}"
                    )
                return resolve(value, seen | {key})
            return value

        return {key: resolve(key, set()) for key in MODEL_ENV_KEYS if key in merged}

    def resolve_model(self, stage: str, environment: dict[str, str]) -> ModelConfig:
        if stage not in STAGE_ENV_PREFIXES:
            raise ValueError("Model stage must be gt, prediction or evaluation")
        prefix = STAGE_ENV_PREFIXES[stage]
        options = getattr(self, f"{stage}_model")
        if self.api_config_dir:
            from tau2.utils.api_config import get_api_configuration

            selected = get_api_configuration(self.api_config_dir).text(
                "INTENT_" + stage.upper()
            )
            options = options.model_copy(
                update={
                    k: v for k, v in selected.args.items() if k in options.model_fields
                }
            )
        model = environment.get(f"{prefix}_MODEL") or None
        api_base = environment.get(f"{prefix}_API_BASE") or None
        return ModelConfig(
            **options.model_dump(include=set(ModelOptions.model_fields)),
            model=model,
            api_base=api_base,
            api_key_env=f"{prefix}_API_KEY",
            stage=stage,
        )

    def load_schema(self) -> dict[str, Any]:
        from jsonschema.validators import validator_for

        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            schema = load_definition(self.definition_set)["schema"]
        elif self.schema_path:
            schema = json.loads(self.schema_path.read_text())
        else:
            raise ValueError("Missing schema definition")
        validator_for(schema).check_schema(schema)
        return schema

    def instructions(self) -> str:
        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            return load_definition(self.definition_set)["instructions"]
        return self.instructions_path.read_text() if self.instructions_path else ""

    def profile(self) -> ScoringProfile:
        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            return ScoringProfile.model_validate(
                load_definition(self.definition_set)["scoring"]
            )
        return (
            ScoringProfile.model_validate_json(self.profile_path.read_text())
            if self.profile_path
            else ScoringProfile()
        )

    def validation(self) -> ValidationProfile:
        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            return ValidationProfile.model_validate(
                load_definition(self.definition_set)["validation"]
            )
        return (
            ValidationProfile.model_validate_json(
                self.validation_profile_path.read_text()
            )
            if self.validation_profile_path
            else ValidationProfile()
        )

    def definition_metadata(self) -> dict[str, Any]:
        if self.definition_set:
            from tau2.diagnostics.intent.definitions import load_definition

            return load_definition(self.definition_set)["manifest"]
        return {"name": "custom", "status": "coverage_unverified"}

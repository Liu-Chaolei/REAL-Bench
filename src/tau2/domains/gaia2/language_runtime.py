"""Resolve Gaia2 English caller context for text, voice, and recorded runs."""

import hashlib
import json
import os
from functools import cached_property
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:
    from tau2.data_model.simulation import Info, RunConfig, UserInfo

from loguru import logger
from pydantic import ConfigDict, Field, TypeAdapter

from tau2.backchannel import BACKCHANNEL_PROMPT_VERSION
from tau2.config import SPELLING_NORMALIZATION_VERSION
from tau2.data_model.language import (
    CallerSource,
    CallMode,
    DomainBinding,
    LanguageArtifactPins,
    LanguagePack,
    LanguageTaskInfo,
    PackModel,
    RuntimePersona,
    TaskMetadata,
    TaskSelection,
    TaskSource,
)
from tau2.data_model.tasks import Task
from tau2.domains.gaia2.language import language_root, load_metadata, load_pack_bytes
from tau2.user.guidelines import GUIDELINE_TEMPLATE_VERSION


class LoadedLanguage(PackModel):
    """A validated domain binding, its task population and exact fingerprints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack: LanguagePack = Field(description="Validated published pack.")
    pack_extension: None = Field(
        default=None, description="Compatibility field; Gaia2 has no extensions."
    )
    domain: str = Field(description="Selected domain.")
    binding: DomainBinding = Field(description="Explicit or default domain binding.")
    mode: CallMode = Field(description="Call mode the binding was validated for.")
    selection: TaskSelection = Field(description="Registry task selection used.")
    tasks: list[Task] = Field(description="Resolved task population.")
    metadata: dict[str, TaskMetadata] = Field(
        description="Explicit task correspondence and caller metadata."
    )
    pack_sha256: str = Field(description="Pack artifact digest.")
    tasks_sha256: str = Field(description="Resolved task population digest.")
    metadata_sha256: str = Field(description="Resolved task metadata digest.")
    scenario: None = Field(
        default=None,
        description="Selected complete scenario snapshot; null when not pinned.",
    )
    scenario_mode: Literal["pack"] = Field(
        default="pack", description="Effective scenario selection."
    )
    localization_map_sha256: None = Field(
        default=None, description="Reviewed regional map content digest."
    )

    @cached_property
    def task_index(self) -> dict[str, Task]:
        """Bound tasks by ID."""
        return {task.id: task for task in self.tasks}

    @cached_property
    def artifact_pins(self) -> LanguageArtifactPins:
        """Pin all selected data and the executable domain rules (computed once)."""
        return LanguageArtifactPins(
            pack_sha256=self.pack_sha256,
            tasks_sha256=self.tasks_sha256,
            metadata_sha256=self.metadata_sha256,
            scenario_mode=self.scenario_mode,
            scenario_sha256=None,
            localization_map_sha256=self.localization_map_sha256,
            localization_revision=None,
            runtime_rules_version=None,
            runtime_rules_sha256=None,
        )


def call_mode(config: "RunConfig") -> CallMode:
    """The call mode of a run config."""
    return "voice" if config.is_voice else "text"


def load_pack(language: str = "en") -> LanguagePack:
    """Read Gaia2's only language pack."""
    return load_pack_bytes(language)[0]


def list_languages() -> list[str]:
    """Return the installed Gaia2 call language."""
    return ["en"] if (language_root() / "en/pack.yaml").is_file() else []


def voice_id_env_key(language: str, persona_id: str) -> str:
    """The ``TAU2_VOICE_ID_<LANG>_<PERSONA>`` variable for one pack persona."""
    return "TAU2_VOICE_ID_" + f"{language}_{persona_id}".replace("-", "_").upper()


def _voice_id_override(language: str, persona_id: str, default: str) -> str:
    """``TAU2_VOICE_ID_<LANG>_<PERSONA>`` replaces a pack persona's voice."""
    key = voice_id_env_key(language, persona_id)
    voice_id = os.environ.get(key)
    if voice_id is None:
        return default
    logger.warning(
        f"Using NON-OFFICIAL voice ID for {language} persona {persona_id!r} "
        f"(from env var {key}); results may not be comparable to released runs."
    )
    return voice_id


def resolve_persona(
    pack: LanguagePack,
    persona_id: str,
    *,
    mode: CallMode,
    binding: Optional[DomainBinding] = None,
) -> RuntimePersona:
    """Bind one pack persona to a call mode and domain without mutating the pack."""
    persona = pack.personas.get(persona_id)
    if persona is None:
        raise ValueError(f"language {pack.language} has no persona {persona_id!r}")
    voice = None
    spellout_section = ""
    if mode == "voice":
        if pack.voice is None or persona.voice is None:
            raise ValueError(
                f"language {pack.language} publishes no voice sections; "
                "it supports text runs only"
            )
        voice = persona.voice.model_copy(
            update={
                "voice_id": _voice_id_override(
                    pack.language, persona_id, persona.voice.voice_id
                )
            },
            deep=True,
        )
        spellout_section = pack.voice.spellout_section
    domain_guidelines = ""
    mode_guidelines = ""
    if binding is not None:
        domain_guidelines = binding.user_guidelines
        mode_guidelines = (
            binding.voice_guidelines if mode == "voice" else binding.text_guidelines
        )
    return RuntimePersona(
        verbosity=persona.verbosity,
        interrupt_tendency=voice.interrupt_tendency if voice is not None else None,
        persona_id=persona_id,
        mode=mode,
        gender=persona.gender,
        locale=persona.locale,
        variety=persona.variety,
        language=pack.language,
        display_language=pack.display_name,
        gender_guidance=persona.gender_guidance,
        pragmatics_clauses=list(persona.pragmatics_clauses),
        user_guidelines="\n\n".join(
            s for s in (pack.user_guidelines, domain_guidelines) if s
        ),
        mode_guidelines=mode_guidelines,
        agent_background_clause=persona.agent_background_clause,
        language_directive=persona.language_directive,
        spellout_section=spellout_section,
        voice=voice,
    )


def _registry_tasks(
    task_set_name: str, task_split_name: Optional[str], task_kwargs: dict[str, str]
) -> list[Task]:
    from tau2.registry import registry

    loader = registry.get_tasks_loader(task_set_name)
    return loader(task_split_name=task_split_name, **task_kwargs)


def _binding_tasks(
    selection: TaskSelection,
) -> tuple[list[Task], dict[str, TaskMetadata]]:
    """Load Gaia2 main or hidden tasks and resolve their caller metadata."""
    task_set = selection.task_set_name or "gaia2"
    if task_set not in {"gaia2", "gaia2_hidden"} or selection.task_kwargs:
        raise ValueError("Gaia2 supports only its main and hidden task sets")
    tasks = _registry_tasks(task_set, selection.task_split_name, {})
    published = load_metadata()
    metadata = {}
    for task in tasks:
        if task.id in published:
            metadata[task.id] = published[task.id]
        else:
            # Derived views keep the source caller facts, never an arbitrary persona.
            known = json.loads(task.user_scenario.instructions.known_info or "{}")
            gender = str(known.get("gender", "")).lower()
            metadata[task.id] = TaskMetadata(
                canonical_task_id=task.id,
                caller_gender=gender if gender in {"male", "female"} else None,
            )
    return tasks, metadata


def load_language(
    language: str,
    domain: str,
    *,
    mode: CallMode,
    selection: Optional[TaskSelection] = None,
) -> LoadedLanguage:
    """Validate every task/metadata mapping and persona before any provider work."""
    if language != "en" or domain != "gaia2":
        raise ValueError("Gaia2 language runtime supports only en/gaia2")
    selection = selection or TaskSelection()
    pack, pack_bytes = load_pack_bytes(language)
    pack_sha256 = hashlib.sha256(pack_bytes).hexdigest()
    binding = pack.binding(domain)
    if mode == "voice" and pack.voice is None:
        raise ValueError(
            f"language {language} publishes no voice sections; it supports text runs only"
        )
    tasks, metadata = _binding_tasks(selection)
    scenario = None
    scenario_mode = "pack"
    map_digest = None
    for task in tasks:
        declared_domain = getattr(task.user_scenario.instructions, "domain", None)
        if declared_domain is not None and declared_domain != domain:
            raise ValueError("task scenario domain disagrees with language binding")
    ids = [task.id for task in tasks]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("language binding has empty or duplicate task IDs")
    if set(ids) != set(metadata):
        raise ValueError("task metadata must cover exactly the bound tasks")
    canonical_ids = [m.canonical_task_id for m in metadata.values()]
    if len(set(canonical_ids)) != len(canonical_ids):
        raise ValueError("duplicate canonical task mapping")
    if binding.caller == CallerSource.PACK:
        for entry in metadata.values():
            if not any(
                entry.caller_gender is None or p.gender == entry.caller_gender
                for p in pack.personas.values()
            ):
                raise ValueError("no persona matches caller gender")
            if entry.persona_id is not None:
                p = pack.personas.get(entry.persona_id)
                if p is None or (
                    entry.caller_gender and p.gender != entry.caller_gender
                ):
                    raise ValueError("explicit persona conflicts with caller metadata")
        # Render every persona's scaffold before constructing any provider.
        from tau2.user.guidelines import render_simulation_guidelines

        for persona_id in pack.personas:
            resolved = resolve_persona(pack, persona_id, mode=mode, binding=binding)
            for use_tools in (False, True):
                render_simulation_guidelines(resolved, use_tools=use_tools)
    tasks_bytes, metadata_bytes = (
        _canonical_json([task_payload(task) for task in tasks]),
        TypeAdapter(dict[str, TaskMetadata]).dump_json(metadata),
    )
    return LoadedLanguage(
        pack=pack,
        pack_extension=None,
        domain=domain,
        binding=binding,
        mode=mode,
        selection=selection,
        scenario=scenario,
        scenario_mode=scenario_mode,
        localization_map_sha256=map_digest,
        tasks=tasks,
        metadata=metadata,
        pack_sha256=pack_sha256,
        tasks_sha256=hashlib.sha256(tasks_bytes).hexdigest(),
        metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest(),
    )


def load_run_language(config: "RunConfig") -> LoadedLanguage:
    """Load the language binding a run config selects."""
    return load_language(
        config.language,
        config.domain,
        mode=call_mode(config),
        selection=run_task_selection(config),
    )


def run_task_selection(config: "RunConfig") -> TaskSelection:
    """The registry task selection a run config requests."""
    return TaskSelection(
        task_set_name=config.task_set_name,
        task_split_name=config.task_split_name,
        task_kwargs={},
    )


def task_payload(task: Task) -> dict:
    """A task's content without load-time message timestamps.

    Initial-state history messages that omit ``timestamp`` get the load time as
    a default, so the same registry task differs between loads; that field is
    not scenario content and is excluded from identity checks and digests.
    """
    payload = task.model_dump(mode="json")
    initial_state = payload.get("initial_state") or {}
    for message in initial_state.get("message_history") or []:
        message.pop("timestamp", None)
    return payload


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def task_digest(task: Task) -> str:
    """Canonical digest of a validated task payload."""
    return hashlib.sha256(_canonical_json(task_payload(task))).hexdigest()


def assign_persona(pack: LanguagePack, entry: TaskMetadata, seed: int) -> str:
    """Deterministic, gender-matched persona choice keyed by canonical task ID."""
    if entry.persona_id is not None:
        return entry.persona_id
    eligible = sorted(
        pid
        for pid, p in pack.personas.items()
        if entry.caller_gender is None or p.gender == entry.caller_gender
    )
    index = int.from_bytes(
        hashlib.sha256(f"{seed}:{entry.canonical_task_id}".encode()).digest()[:8],
        "big",
    ) % len(eligible)
    return eligible[index]


def resolve_task(loaded: LoadedLanguage, task: Task, seed: int) -> LanguageTaskInfo:
    """Resolve one task against its binding.

    File-published populations are closed: only their exact tasks run. Registry
    populations are open, as ordinary tau2 task sets are: a caller-supplied or
    edited task runs under its registry metadata (or a canonical default), and
    its exact payload is pinned by ``task_sha256``.
    """
    expected = loaded.task_index.get(task.id)
    if loaded.binding.source == TaskSource.REGISTRY:
        entry = loaded.metadata.get(task.id) or TaskMetadata(canonical_task_id=task.id)
    elif expected is None or task_payload(expected) != task_payload(task):
        raise ValueError("task does not match published language binding")
    else:
        entry = loaded.metadata[task.id]
    pack = loaded.pack
    persona = None
    if loaded.binding.caller == CallerSource.PACK:
        persona = resolve_persona(
            pack,
            assign_persona(pack, entry, seed),
            mode=loaded.mode,
            binding=loaded.binding,
        )
    voice = pack.voice if loaded.mode == "voice" else None
    tables = (
        persona.voice.spelled_entity_tables
        if persona is not None and persona.voice is not None
        else None
    )
    guidance = None
    return LanguageTaskInfo(
        domain_agent_guidance=guidance,
        language=pack.language,
        pack_extension=loaded.pack_extension,
        mode=loaded.mode,
        cross_lingual=False,
        pack_revision=pack.revision,
        **loaded.artifact_pins.model_dump(),
        domain_revision=loaded.binding.revision,
        task_selection=loaded.selection,
        task_sha256=task_digest(task),
        canonical_task_id=entry.canonical_task_id,
        agent_greeting=(voice.agent_greeting if voice is not None else None)
        or pack.agent_greeting,
        agent_language_clause=pack.agent_language_clause,
        speech_insert_events_per_minute=voice.speech_insert_events_per_minute
        if voice is not None
        else None,
        backchannel_level=voice.backchannel_level if voice is not None else None,
        backchannel_prompt_version=BACKCHANNEL_PROMPT_VERSION
        if voice is not None
        else None,
        caller_gender=entry.caller_gender,
        persona=persona,
        guideline_template_version=GUIDELINE_TEMPLATE_VERSION,
        spelling_normalization_version=SPELLING_NORMALIZATION_VERSION
        if tables is not None
        else None,
    )


def without_delivery_details(context: LanguageTaskInfo) -> LanguageTaskInfo:
    """A context with delivery-only details (the TTS voice ID) blanked.

    ``TAU2_VOICE_ID_*`` overrides change the persona's synthesized voice, not the
    task, persona behavior or scenario, so equality checks that decide whether a
    recorded call still matches its binding compare contexts through this.
    """
    persona = context.persona
    if persona is None or persona.voice is None:
        return context
    voice = persona.voice.model_copy(update={"voice_id": "-"})
    return context.model_copy(
        update={"persona": persona.model_copy(update={"voice": voice})}
    )


def pin_language_artifacts(
    config: "RunConfig", *, update: bool = True
) -> LoadedLanguage:
    """Validate file digests and return the same snapshot for task selection."""
    loaded = load_run_language(config)
    pins = loaded.artifact_pins
    if config.language_artifacts is not None and config.language_artifacts != pins:
        raise ValueError("published language artifacts changed during this run")
    if update:
        config.language_artifacts = pins
        config.pack_extension = loaded.pack_extension
    return loaded


def task_context(
    config: "RunConfig", task: Task, *, context: LanguageTaskInfo | None = None
) -> LanguageTaskInfo:
    """Resolve a standalone call or check a prepared call's recorded snapshot.

    Prepared calls retain their exact persona assignment. Revalidate the complete
    selected scenario before every attempt, including its DB, policies and domain
    rules. This is a call boundary, not a streaming-tick operation.
    """
    if config.benchmark is not None and (
        config.language != config.benchmark.language
        or config.domain != config.benchmark.domain
    ):
        raise ValueError("benchmark calls require the recorded language and domain")
    loaded = pin_language_artifacts(config, update=False)
    if context is None:
        return resolve_task(loaded, task, config.seed or 0)
    if context.language != config.language or context.mode != call_mode(config):
        raise ValueError("prepared language context does not match this run")
    tables = (
        context.persona.voice.spelled_entity_tables
        if context.persona is not None and context.persona.voice is not None
        else None
    )
    expected_normalization = (
        SPELLING_NORMALIZATION_VERSION if tables is not None else None
    )
    if context.spelling_normalization_version != expected_normalization:
        raise ValueError(
            "prepared language context has a different spelling normalization revision"
        )
    if loaded.artifact_pins != context.artifact_pins:
        raise ValueError("published language artifacts changed during this run")
    if task_digest(task) != context.task_sha256:
        raise ValueError("task does not match prepared language context")
    return context


def language_review_user_info(
    user_info: "UserInfo",
    task: Task,
    context: LanguageTaskInfo,
    *,
    use_tools: bool,
) -> "UserInfo":
    """Give reviewers the same owned instructions and behavior as this caller.

    Reviewers consume guidelines but do not render persona_config themselves.
    The task scenario is supplied separately, so only persona guidance and its
    final reminder are included here. No current pack is read.
    """
    from tau2.user.gaia2_guidelines import get_gaia2_user_guidelines

    if task_digest(task) != context.task_sha256:
        raise ValueError("Recorded language review context does not match task")
    persona = context.persona
    if persona is None:
        return user_info
    guidelines = get_gaia2_user_guidelines(
        persona, mode=context.mode, use_tools=use_tools
    )
    return user_info.model_copy(
        update={
            "global_simulation_guidelines": guidelines,
            "persona_config": persona,
        }
    )


def recorded_review_user_info(
    info: "Info",
    task: Task,
    *,
    context: LanguageTaskInfo | None = None,
) -> "UserInfo":
    """Resolve saved task-specific guidelines, retaining ordinary/domain overrides."""
    context = context or info.language_tasks.get(task.id)
    if context is None:
        if info.language is None:
            # Results recorded before every run resolved a language context.
            return info.user_info
        raise ValueError("Missing recorded language review context for this task")
    if not isinstance(context, LanguageTaskInfo):
        raise ValueError(
            "Recorded language context uses a retired schema; its caller prompt "
            "cannot be re-rendered for review"
        )
    if info.language is not None and context.language != info.language:
        raise ValueError("Mismatched recorded language review context")

    # Task None means all domain user tools, whereas [] explicitly disables them.
    use_tools = False
    if task.user_tools != []:
        from tau2.registry import registry

        kwargs = {}
        if info.retrieval_config is not None:
            kwargs = {"retrieval_variant": info.retrieval_config, "task": task}
            if info.retrieval_config_kwargs:
                kwargs["retrieval_kwargs"] = dict(info.retrieval_config_kwargs)
        environment = registry.get_env_constructor(info.environment_info.domain_name)(
            **kwargs
        )
        if environment.user_tools is not None:
            use_tools = bool(environment.get_user_tools(include=task.user_tools))
    return language_review_user_info(info.user_info, task, context, use_tools=use_tools)


def agent_language_guidance(
    context: LanguageTaskInfo, *, agent_voice_gender: Optional[str] = None
) -> str:
    """Render explicit language and actual selected voice context for an agent."""
    background = context.persona.agent_background_clause if context.persona else ""
    guidance = (
        context.domain_agent_guidance.text if context.domain_agent_guidance else ""
    )
    clause = "\n".join(
        s for s in (background, context.agent_language_clause, guidance) if s
    )
    if agent_voice_gender is not None:
        clause += f"\nYour gender is {agent_voice_gender}."
    return clause


def validate_language_resume(previous: "Info", current: "Info") -> None:
    """Never combine trajectories generated with different published content."""
    if previous.language is None and current.language is not None:
        raise ValueError(
            "saved results predate the language schema (no recorded language); "
            "they cannot be resumed and must be re-run into a new output path"
        )
    if previous.language != current.language:
        raise ValueError(
            "cannot resume with a different language; use a new output path"
        )
    for task_id, context in previous.language_tasks.items():
        if current.language_tasks.get(task_id) != context:
            raise ValueError(
                "cannot resume changed language/task/persona content; use a new output path"
            )
    # Selection may grow, but existing and future calls must use the same
    # effective settings. Voice changes also change the agent's gender instruction.
    selection_and_provenance = {
        "git_commit",
        "language_tasks",
        "num_trials",
        "benchmark",
        "communicate_scoring",
    }
    before = previous.model_dump(mode="json", exclude=selection_and_provenance)
    after = current.model_dump(mode="json", exclude=selection_and_provenance)
    changed = sorted(key for key in before if before[key] != after[key])
    if changed:
        raise ValueError(
            "language run conditions changed ("
            + ", ".join(changed)
            + "); use a new output path"
        )
    if current.num_trials < previous.num_trials:
        raise ValueError("cannot resume a language run with fewer trials")


def language_environment_kwargs(loaded: LoadedLanguage) -> dict:
    """Provide independent database instances and the matching selected policy."""
    return {}


def reload_call_language(context: LanguageTaskInfo, domain: str) -> LoadedLanguage:
    """Reload exactly the binding a recorded call used, rejecting any drift."""
    loaded = load_language(
        context.language,
        domain,
        mode=context.mode,
        selection=context.task_selection,
    )
    if loaded.artifact_pins != context.artifact_pins:
        raise ValueError("published language artifacts differ from the recorded call")
    return loaded

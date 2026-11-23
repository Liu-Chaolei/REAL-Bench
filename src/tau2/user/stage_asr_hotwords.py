"""Load reviewed fixed stage hotwords without calling an extraction model."""

import hashlib
import json
import re

from tau2.config import DEFAULT_GAIA2_STAGE_HOTWORDS_DIRECTORY
from tau2.data_model.asr import (
    HotwordEntry,
    HotwordSettings,
    StagedHotwordProfile,
    StageHotwordVocabulary,
)
from tau2.data_model.tasks import Task
from tau2.user.asr_hotwords import normalized
from tau2.utils.utils import DATA_DIR


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _strings(value, prefix):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, prefix + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, prefix + f"[{index}]")


def stage_hotword_input(task: Task) -> dict:
    """Project exactly the source/GT material used in the offline extraction."""
    if task.scenario is None or not task.scenario.stages:
        raise ValueError("Stage hotwords require a staged scenario")
    events = {event.id: event for event in task.scenario.events}
    stages = []
    for stage in task.scenario.stages:
        sources = {f"stage{stage.stage}.source_task": stage.request}
        records = []
        for ident in stage.event_ids:
            event = events[ident]
            if event.kind != "reference":
                continue
            role = "reference_reply" if event.channel == "agent" else "tool_gt"
            prefix = f"stage{stage.stage}.{role}.{ident}.arguments"
            sources.update(_strings(event.arguments, prefix))
            records.append({"event_id": ident, "tool": event.tool, "role": role})
        stages.append({"stage": stage.stage, "sources": sources, "gt_records": records})
    return {"task_id": task.id, "stages": stages}


def load_stage_hotword_profile(
    task: Task, settings: HotwordSettings
) -> StagedHotwordProfile:
    """Reject stale, missing or ungrounded catalogs before billable startup."""
    directory = (
        settings.catalog_dir or DATA_DIR / DEFAULT_GAIA2_STAGE_HOTWORDS_DIRECTORY
    )
    manifest = json.loads((directory / "manifest.json").read_text())
    # Task IDs are lookup keys, never arbitrary relative filenames.
    if not task.id or "/" in task.id or "\\" in task.id or task.id in {".", ".."}:
        raise ValueError("Invalid task ID for the ASR catalog")
    path = directory / "tasks" / f"{task.id}.json"
    profile = json.loads(path.read_text())
    material = stage_hotword_input(task)
    if manifest["version"] == "gaia2-stage-hotwords-v2":
        return _load_reviewed_profile(task, settings, manifest, profile, material)
    if (
        profile["task_id"] != task.id
        or profile["input_sha256"] != _digest(material)
        or profile["prompt_sha256"] != manifest["prompt_sha256"]
        or profile["model"] != manifest["model"]
        or profile["version"] != manifest["version"]
        or profile["weight"] != 50
        or manifest["weight"] != 50
    ):
        raise ValueError("ASR catalog does not match the current task/source version")
    if [row["stage"] for row in profile["stages"]] != [
        row["stage"] for row in material["stages"]
    ]:
        raise ValueError("ASR catalog stages are incomplete or reordered")
    active, rows = {}, []
    for row, supplied in zip(profile["stages"], material["stages"], strict=True):
        local, added = set(), []
        for term in row["extracted"]:
            phrase = normalized(term["text"])
            source = supplied["sources"].get(term["source"])
            if (
                not phrase
                or len(phrase) > 80
                or "@" in phrase
                or not any(c.isalpha() for c in phrase)
                or source is None
                or phrase not in normalized(source)
            ):
                raise ValueError(
                    "ASR term is not grounded in its declared stage source"
                )
            key = phrase.casefold()
            if key in local:
                raise ValueError("Duplicate stage ASR term")
            local.add(key)
            if key not in active:
                active[key] = {"text": phrase, "sources": {term["source"]}}
                added.append(phrase)
            else:
                active[key]["sources"].add(term["source"])
        vocabulary = {entry["text"]: 50 for entry in active.values()}
        if (
            vocabulary != row["active_vocabulary"]
            or added != row["new_terms"]
            or _digest(vocabulary) != row["vocabulary_sha256"]
            or len(vocabulary) > min(settings.max_terms, 50)
        ):
            raise ValueError("ASR catalog violates cumulative vocabulary rules")
        rows.append(
            StageHotwordVocabulary(
                stage=row["stage"],
                entries=tuple(
                    HotwordEntry(
                        text=entry["text"],
                        weight=50,
                        sources=tuple(sorted(entry["sources"])),
                    )
                    for entry in active.values()
                ),
                new_terms=tuple(added),
                vocabulary_sha256=_digest(vocabulary),
            )
        )
    return StagedHotwordProfile(
        version=profile["version"],
        task_id=task.id,
        input_sha256=profile["input_sha256"],
        prompt_sha256=profile["prompt_sha256"],
        extractor_model=profile["model"],
        catalog_sha256=_digest(manifest),
        stages=tuple(rows),
    )


def _load_reviewed_profile(task, settings, manifest, profile, material):
    """Verify frozen tool-response evidence and cumulative mixed weights."""
    if (
        not manifest.get("complete")
        or profile["task_id"] != task.id
        or profile["input_sha256"] != _digest(material)
        or profile["prompt_sha256"] != manifest["prompt_sha256"]
        or profile["model"] != manifest["model"]
        or profile["version"] != manifest["version"]
        or profile["weight"] != manifest["weight"]
        or profile["weight"] != 50
        or profile.get("overflow_weight") != manifest.get("overflow_weight")
        or profile.get("overflow_weight") != 5
        or profile.get("weight_policy") != manifest.get("weight_policy")
        or profile.get("weight_policy") != "first-50-cumulative-v1"
        or _digest(profile) != manifest.get("profiles_sha256", {}).get(task.id)
    ):
        raise ValueError(
            "Reviewed ASR catalog does not match the current task/source version"
        )
    if [s["stage"] for s in profile["stages"]] != [
        s["stage"] for s in material["stages"]
    ]:
        raise ValueError("Reviewed ASR catalog stages are incomplete or reordered")
    sources = profile["sources"]
    active, rows = {}, []
    for row, supplied in zip(profile["stages"], material["stages"], strict=True):
        number = row["stage"]
        references = {
            r["event_id"] for r in supplied["gt_records"] if r["role"] == "tool_gt"
        }
        local, added = set(), []
        for term in row["extracted"]:
            phrase, key = normalized(term["text"]), term["source"]
            evidence = sources.get(key)
            if (
                not phrase
                or len(phrase) > 80
                or "@" in phrase
                or not any(c.isalpha() for c in phrase)
                or any(ord(c) < 32 for c in phrase)
                or evidence is None
                or evidence["stage"] != number
                or phrase not in normalized(evidence["text"])
            ):
                raise ValueError("Reviewed ASR term lacks literal same-stage evidence")
            if "original_source" in evidence:
                original = supplied["sources"].get(evidence["original_source"])
                if (
                    not key.startswith(f"stage{number}.existing_catalog.")
                    or original is None
                    or normalized(evidence["text"]) not in normalized(original)
                ):
                    raise ValueError(
                        "Reviewed old ASR term is not grounded in task text/GT"
                    )
            else:
                ident = evidence.get("event_id")
                prefix = f"stage{number}.tool_response.{ident}"
                if (
                    ident not in references
                    or not (
                        key == prefix
                        or re.fullmatch(re.escape(prefix) + r"\.chunk\d+", key)
                    )
                    or not re.fullmatch(
                        r"[0-9a-f]{64}", evidence.get("response_sha256", "")
                    )
                ):
                    raise ValueError(
                        "Reviewed ASR tool evidence belongs to a different stage/event"
                    )
            weight = (
                active[phrase.casefold()]["weight"]
                if phrase.casefold() in active
                else (50 if len(active) < 50 else 5)
            )
            if term["weight"] != weight or phrase.casefold() in local:
                raise ValueError(
                    "Reviewed ASR term has invalid weight or is duplicated"
                )
            local.add(phrase.casefold())
            if phrase.casefold() not in active:
                active[phrase.casefold()] = {
                    "text": phrase,
                    "weight": weight,
                    "sources": {key},
                }
                added.append(phrase)
            else:
                entry = active[phrase.casefold()]
                entry["sources"].add(key)
        vocabulary = {e["text"]: e["weight"] for e in active.values()}
        if (
            vocabulary != row["active_vocabulary"]
            or added != row["new_terms"]
            or _digest(vocabulary) != row["vocabulary_sha256"]
            or len(vocabulary) > min(settings.max_terms, 2000)
            or sum(w == 50 for w in vocabulary.values()) > 50
        ):
            raise ValueError(
                "Reviewed ASR catalog violates cumulative vocabulary rules"
            )
        rows.append(
            StageHotwordVocabulary(
                stage=number,
                entries=tuple(
                    HotwordEntry(
                        text=e["text"],
                        weight=e["weight"],
                        sources=tuple(sorted(e["sources"])),
                    )
                    for e in active.values()
                ),
                new_terms=tuple(added),
                vocabulary_sha256=_digest(vocabulary),
            )
        )
    return StagedHotwordProfile(
        version=profile["version"],
        task_id=task.id,
        input_sha256=profile["input_sha256"],
        prompt_sha256=profile["prompt_sha256"],
        extractor_model=profile["model"],
        catalog_sha256=_digest(manifest),
        stages=tuple(rows),
    )

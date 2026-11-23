"""Extract only incremental ASR hotwords from recorded Gaia2 tool responses."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extract_gaia2_stage_hotwords import (  # noqa: E402
    CONFIG,
    TASKS,
    digest,
    parse,
    task_input,
    validate,
)

PROMPT_INCREMENTAL = """Extract only NEW difficult proper names for an observer ASR
hotword vocabulary from recorded Gaia2 tool responses.

The input contains ONLY serialized tool-response values that were not included
in the existing source-only extraction. Do not infer anything from tool IDs,
phone numbers, internal identifiers, or surrounding task context. Select
people, places, organizations, brands/models, unusual technical terms, and
distinctive custom names only when the exact contiguous spelling occurs in a
same-stage supplied value. Preserve accents and punctuation in the quotation.
Exclude ordinary words, dates, times, prices, phone numbers, email addresses,
IDs, tool names, and full sentences. Do not fill a quota; an empty stage is
valid. Do not compress the vocabulary merely to reach a small target count.
Retain every clearly difficult complete proper name or place name up to the
50-term stage limit, and remove only clearly ordinary, generic, duplicate, or
covered terms. Do not enumerate a long list of easy fields.
Common short given names (for example ``Max``), generic pet/breed words,
nationality or language adjectives, and generic words such as ``department``
are not useful hotwords unless they form part of a distinctive full name.
When a full name is available, do not also select its first name or surname.
Treat a complete proper name, place, organization, or custom name containing
Latin diacritics as high-priority: retain it unless it is merely a generic word,
numeric field, or address suffix. Examples include ``Šárka Nováková``,
``Křižovnická``, and ``Matěj Nováček``. A difficult complete name without
diacritics, such as ``Akseli Korhonen``, remains eligible as well.

Few-shot selection example:
  Tool response: ``Max, my Labrador Retriever ... Matěj Nováček ...``
  Good: ``Matěj Nováček``
  Bad: ``Max``, ``Labrador Retriever``, or ``Matěj``
The good choice is a complete uncommon name likely to be misrecognized; the
ordinary short name and generic breed are omitted. Apply the same principle
to names and labels in long tool-result lists.

Return every supplied stage exactly once, in order, with at most 50 terms per
stage.

Each term must cite its exact supplied source key. Return JSON only:
{"stages":[{"stage":0,"terms":[{"text":"literal name",
"kind":"person/place/organization/product/technical_term/custom_name",
"source":"exact supplied source key","reason":"brief Chinese reason"}]}]}.
"""

PROMPT_REVIEW = """Review an existing observer ASR hotword vocabulary and add only
new difficult proper names found in recorded Gaia2 tool responses.

Sources prefixed ``*.existing_catalog`` are terms already selected by the
source-only extractor. Sources prefixed ``*.tool_response`` are newly supplied
tool-response values. Return the FINAL stage vocabulary for each stage, bounded
only by the 50-term provider limit: review
both source types, omit ordinary or easy terms, and include a term only when
its exact spelling occurs in the cited same-stage source. Do not invent
aliases, and do not enumerate a long result list of easy fields. Retain every
clearly difficult complete proper name or place name up to the 50-term stage
limit; never return more than 50 terms in a stage. Common short names such as ``Max``,
generic breed words, ordinary nationality adjectives, and generic institution
words are not useful unless they are part of a distinctive difficult name.
When a full name is available, omit its first name and surname separately.
Treat a complete proper name, place, organization, or custom name containing
Latin diacritics as high-priority: retain it unless it is merely a generic word,
numeric field, or address suffix. Examples include ``Šárka Nováková``,
``Křižovnická``, and ``Matěj Nováček``. A difficult complete name without
diacritics, such as ``Akseli Korhonen``, remains eligible as well.

Few-shot:
  Existing candidates: ``Max``, ``Prague Police Department``
  New tool result: ``Matěj Nováček``
  Good final selection: ``Matěj Nováček``
  Drop: ``Max`` and ``Prague Police Department``

Return JSON only with every stage once, in order:
{"stages":[{"stage":0,"terms":[{"text":"literal name",
"kind":"person/place/organization/product/technical_term/custom_name",
"source":"exact supplied source key","reason":"brief Chinese reason"}]}]}.
"""


def response_payload(task: dict, run: dict, baseline: dict | None = None) -> dict:
    """Build a payload containing only tool-response content by stage."""
    base = task_input(task)
    events = {event["event_id"]: event for event in run["events"]}
    stages = []
    for stage in base["stages"]:
        sources = {}
        for record in stage["gt_records"]:
            event_id = record["event_id"]
            response = events[event_id].get("tool_response") or {}
            content = response.get("content")
            if not content:
                continue
            try:
                value = json.loads(content)
                # Keep object keys and values in the eligible source.  A name
                # returned by a lookup is often a JSON object key, which the
                # normal recursive value-only flattener would omit.
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                text = content
            sources[f"stage{stage['stage']}.tool_response.{event_id}"] = text
        if baseline is not None:
            for index, term in enumerate(
                baseline["stages"][stage["stage"]]["new_terms"]
            ):
                sources[f"stage{stage['stage']}.existing_catalog.{index}"] = term
        stages.append({"stage": stage["stage"], "sources": sources, "gt_records": []})
    return {"task_id": task["id"], "stages": stages}


def all_terms(profile: dict) -> set[str]:
    return {
        term["text"].casefold()
        for stage in profile["stages"]
        for term in stage["extracted"]
    }


def proposed_weights(
    terms_by_stage: list[list[str]], baseline: dict, review_existing: bool = False
) -> list[dict[str, int]]:
    """Assign 50 to the first 50 cumulative terms and 5 only to overflow."""
    active, rows = {}, []
    for index, terms in enumerate(terms_by_stage):
        candidates = (
            terms
            if review_existing
            else [*baseline["stages"][index]["active_vocabulary"], *terms]
        )
        for term in candidates:
            key = term.casefold()
            if key not in active:
                active[key] = 50 if len(active) < 50 else 5
        rows.append({term: active[term.casefold()] for term in terms})
    return rows


def word_tokens(text: str) -> tuple[str, ...]:
    """Normalize a phrase for deterministic word-sequence coverage checks."""
    normalized = unicodedata.normalize("NFC", text).casefold()
    return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def covered_by(candidate: str, active_terms: set[str]) -> bool:
    """Return whether candidate is an exact contiguous phrase inside a hotword."""
    candidate_tokens = word_tokens(candidate)
    if not candidate_tokens:
        return True
    for existing in active_terms:
        existing_tokens = word_tokens(existing)
        width = len(candidate_tokens)
        if len(existing_tokens) >= width and any(
            existing_tokens[index : index + width] == candidate_tokens
            for index in range(len(existing_tokens) - width + 1)
        ):
            return True
    return False


def filter_incremental_terms(
    generated: list[list[dict]], baseline: dict
) -> tuple[list[list[str]], list[list[str]]]:
    """Drop exact covered phrases and shorter aliases, stage by stage."""
    active = [set(stage["active_vocabulary"]) for stage in baseline["stages"]]
    kept_by_stage, removed_by_stage = [], []
    cumulative_new: set[str] = set()
    for index, stage in enumerate(generated):
        active_terms = set(active[index]) | cumulative_new
        candidates = sorted(
            (term["text"] for term in stage["extracted"]),
            key=lambda text: (-len(word_tokens(text)), text.casefold()),
        )
        kept, removed = [], []
        for candidate in candidates:
            if covered_by(candidate, active_terms | set(kept)):
                removed.append(candidate)
            else:
                kept.append(candidate)
        kept_by_stage.append(kept)
        removed_by_stage.append(removed)
        cumulative_new.update(kept)
    return kept_by_stage, removed_by_stage


def review_vocabulary(
    generated: list[dict], baseline: dict
) -> tuple[list[list[str]], list[list[str]], list[list[str]], list[list[str]]]:
    """Review old terms and filter new terms while preserving stage activation."""
    old_by_stage = [
        [term for term in stage["new_terms"]] for stage in baseline["stages"]
    ]
    all_old = {term.casefold() for terms in old_by_stage for term in terms}
    active_old: set[str] = set()
    dropped_old: set[str] = set()
    cumulative_new: set[str] = set()
    final_by_stage, removed_by_stage, dropped_by_stage, exact_new_by_stage = (
        [],
        [],
        [],
        [],
    )

    for index, stage in enumerate(generated):
        current_old = {term.casefold() for term in old_by_stage[index]}
        eligible_old = active_old | current_old
        raw_terms = stage["extracted"]
        raw_keys = {term["text"].casefold() for term in raw_terms}
        retained = [
            term["text"]
            for term in raw_terms
            if term["text"].casefold() in eligible_old
        ]
        dropped = [
            term for term in old_by_stage[index] if term.casefold() not in raw_keys
        ]
        dropped_old.update(term.casefold() for term in dropped)
        active_old.update(term.casefold() for term in retained)

        candidates = [
            term["text"]
            for term in raw_terms
            if term["text"].casefold() not in eligible_old
            and term["text"].casefold() not in dropped_old
        ]
        exact_new_by_stage.append(candidates)
        active_terms = set(active_old) | cumulative_new
        kept, removed = [], []
        for candidate in sorted(
            candidates,
            key=lambda text: (-len(word_tokens(text)), text.casefold()),
        ):
            if covered_by(candidate, active_terms | set(kept) | set(all_old)):
                removed.append(candidate)
            else:
                kept.append(candidate)
        cumulative_new.update(kept)
        final_by_stage.append(retained + kept)
        removed_by_stage.append(removed)
        dropped_by_stage.append(dropped)

    return final_by_stage, removed_by_stage, dropped_by_stage, exact_new_by_stage


async def call_ds(
    payload: dict, model: str, endpoint: str, key: str, prompt: str
) -> dict:
    base_request = {
        "model": model,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "seed": 42,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    attempts = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(150, connect=15), trust_env=False
    ) as client:
        for attempt in range(1, 4):
            request = {**base_request, "messages": messages}
            content = None
            try:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + key},
                    json=request,
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                extraction = parse(content)
                stages = validate(extraction, payload)
                attempts.append({"attempt": attempt, "status": "accepted"})
                return {
                    "usage": body.get("usage"),
                    "finish_reason": body["choices"][0].get("finish_reason"),
                    "stages": stages,
                    "raw_response": content,
                    "attempts": attempts,
                }
            except Exception as error:
                detail = str(error)[:1200]
                attempts.append(
                    {
                        "attempt": attempt,
                        "status": "rejected",
                        "error_type": type(error).__name__,
                        "error": detail,
                    }
                )
                if attempt == 3:
                    raise
                correction = (
                    "Correct the previous response. Return ONLY the JSON object "
                    "with key 'stages'; each stage must contain only 'stage' and "
                    "'terms'. Do not include gt_records or any other fields. "
                    "Return every stage once, in order, and keep every clearly "
                    "difficult complete proper-name term up to 50; remove only "
                    "clear ordinary or duplicate terms. Do not enumerate easy "
                    "fields. Validation error: " + detail
                )
                messages = [
                    *messages,
                    *([{"role": "assistant", "content": content}] if content else []),
                    {"role": "user", "content": correction},
                ]
                await asyncio.sleep(1)


async def main(args: argparse.Namespace) -> None:
    from tau2.utils.env_config import read_config_environment, resolve_env_reference

    tasks = json.loads(TASKS.read_text())
    task = next(task for task in tasks if task["id"] == args.task_id)
    run_path = Path(args.gt_results) / "tasks" / f"{args.task_id}.json"
    run = json.loads(run_path.read_text())
    profile_path = ROOT / "data/gaia2_stage_hotwords/v1/tasks" / f"{args.task_id}.json"
    baseline = json.loads(profile_path.read_text())
    payload = response_payload(task, run, baseline if args.review_existing else None)

    config = json.loads(CONFIG.read_text())
    values = read_config_environment(config["config_env_file"])
    model = resolve_env_reference(values, config["user_llm_model_env"]).removeprefix(
        "openai/"
    )
    key = resolve_env_reference(values, config["user_llm_api_key_env"])
    endpoint = (
        resolve_env_reference(values, config["user_llm_base_url_env"]).rstrip("/")
        + "/chat/completions"
    )
    prompt = PROMPT_REVIEW if args.review_existing else PROMPT_INCREMENTAL
    generated = await call_ds(payload, model, endpoint, key, prompt)
    baseline_terms = all_terms(baseline)
    if args.review_existing:
        (
            reviewed_final,
            phrase_removed,
            dropped_existing,
            exact_incremental,
        ) = review_vocabulary(generated["stages"], baseline)
        filtered_incremental = [
            [term for term in stage if term.casefold() not in baseline_terms]
            for stage in reviewed_final
        ]
    else:
        exact_incremental = [
            [
                term["text"]
                for term in stage["extracted"]
                if term["text"].casefold() not in baseline_terms
            ]
            for stage in generated["stages"]
        ]
        filtered_incremental, phrase_removed = filter_incremental_terms(
            generated["stages"], baseline
        )
        reviewed_final = None
        dropped_existing = None
    output = {
        "task_id": args.task_id,
        "model": model,
        "review_existing": args.review_existing,
        "input_sha256": digest(payload),
        "tool_response_source_counts": [
            len(stage["sources"]) for stage in payload["stages"]
        ],
        "existing_source_only_terms": [
            [term["text"] for term in stage["extracted"]]
            for stage in baseline["stages"]
        ],
        "toolresponse_terms": [
            [term["text"] for term in stage["extracted"]]
            for stage in generated["stages"]
        ],
        "reviewed_final_vocabulary": reviewed_final,
        "dropped_existing_terms": dropped_existing,
        "new_terms_after_exact_deduplication": exact_incremental,
        "new_terms_after_phrase_filter": filtered_incremental,
        "removed_by_phrase_coverage": phrase_removed,
        "proposed_vocabulary_weights": proposed_weights(
            reviewed_final if args.review_existing else filtered_incremental,
            baseline,
            review_existing=args.review_existing,
        ),
        "weight_note": (
            "The first 50 cumulative phrases use 50, independent of source; "
            "only later additions use 5. Previously activated weights stay fixed."
        ),
        "usage": generated["usage"],
        "finish_reason": generated["finish_reason"],
        "attempts": generated["attempts"],
        "raw_response": generated["raw_response"],
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--review-existing",
        action="store_true",
        help="Include the existing source-only vocabulary for LLM review",
    )
    parser.add_argument(
        "--gt-results",
        type=Path,
        default=ROOT / "data/gaia2_stage_hotwords/v1/gt_results",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "data/simulations/gaia2-toolresponse-hotwords-test-20261122/result.json",
    )
    asyncio.run(main(parser.parse_args()))

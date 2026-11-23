"""Review all fixed Gaia2 hotwords and extract tool-response additions with DS.

Keep the v1 catalog untouched. Persist raw requests/replies privately and build
a self-contained v2 catalog with same-stage evidence and mixed 50/5 weights.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_gaia2_stage_hotwords import (  # noqa: E402
    CONFIG,
    ROOT,
    TASKS,
    digest,
    normalize,
    parse,
    save,
    task_input,
)

VERSION = "gaia2-stage-hotwords-v2"
PROMPT = (
    Path(__file__)
    .with_name("gaia2_toolresponse_hotwords_prompt.txt")
    .read_text()
    .strip()
)
BASELINE = ROOT / "data/gaia2_stage_hotwords/v1"
RESULTS = BASELINE / "gt_results"
OUTPUT = ROOT / "data/gaia2_stage_hotwords/v2"
ARTIFACTS = ROOT / "data/simulations/gaia2-hotwords-v2-1100-20261122"
PART_CHARS = 48000
WEIGHT_POLICY = "first-50-cumulative-v1"


def tokens(text):
    """Compare whole words while preserving diacritics and ignoring punctuation."""
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold()))


def covering(phrase, existing):
    """Find an actually retained phrase that covers the candidate words."""
    needle = tokens(phrase)
    for other in existing:
        haystack = tokens(other)
        if needle and any(
            haystack[i : i + len(needle)] == needle
            for i in range(len(haystack) - len(needle) + 1)
        ):
            return other
    return None


def textual(value, omitted, path="$ "):
    """Remove binary base64 leaves without dropping surrounding textual fields."""
    if isinstance(value, str) and len(value) > 1000:
        stripped = value.removeprefix("data:")
        if value.startswith("data:") and ";base64," in value:
            stripped = value.split(";base64,", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", stripped):
            omitted.append({"path": path, "chars": len(value), "sha256": digest(value)})
            return "[binary attachment omitted]"
    if isinstance(value, dict):
        return {
            key: textual(item, omitted, path + "." + key) for key, item in value.items()
        }
    if isinstance(value, list):
        return [textual(item, omitted, path + f"[{i}]") for i, item in enumerate(value)]
    return value


def prepare(task):
    """Build source lists and exact evidence without resending original task text."""
    old = json.loads((BASELINE / "tasks" / f"{task['id']}.json").read_text())
    material = task_input(task)
    if old["input_sha256"] != digest(material):
        raise ValueError(f"Stale baseline: {task['id']}")
    result_path = RESULTS / "tasks" / f"{task['id']}.json"
    run = json.loads(result_path.read_text())
    if (
        run["task_id"] != task["id"]
        or run["source_sha256"] != task["scenario"]["source_sha256"]
        or run["snapshot"] != task["scenario"]["snapshot"]
    ):
        raise ValueError(f"Stale reference results: {task['id']}")
    events = {e["event_id"]: e for e in run["events"]}
    evidence, omitted, stages = {}, [], []
    for supplied, baseline in zip(material["stages"], old["stages"], strict=True):
        number = supplied["stage"]
        sources = {}
        for index, phrase in enumerate(baseline["new_terms"]):
            original = next(t for t in baseline["extracted"] if t["text"] == phrase)
            name = f"stage{number}.existing_catalog.{index}"
            sources[name] = phrase
            evidence[name] = {
                "stage": number,
                "text": phrase,
                "original_source": original["source"],
            }
        for record in supplied["gt_records"]:
            event = events[record["event_id"]]
            response = event.get("tool_response") or {}
            if response.get("error"):
                raise ValueError("Reference response contains an error")
            content = response.get("content")
            if not content:
                continue
            try:
                value = json.loads(content)
            except (ValueError, TypeError):
                value = content
            clean = textual(value, omitted)
            text = (
                clean
                if isinstance(clean, str)
                else json.dumps(clean, ensure_ascii=False)
            )
            # Split large textual returns with overlap; never silently truncate.
            offset, chunk = 0, 0
            while offset < len(text):
                end = min(offset + PART_CHARS - 1000, len(text))
                if end < len(text):
                    boundary = text.rfind(" ", offset + PART_CHARS // 2, end)
                    if boundary > offset:
                        end = boundary
                name = f"stage{number}.tool_response.{event['event_id']}"
                if len(text) > PART_CHARS - 1000:
                    name += f".chunk{chunk}"
                sources[name] = text[offset:end]
                evidence[name] = {
                    "stage": number,
                    "text": text[offset:end],
                    "event_id": event["event_id"],
                    "response_sha256": digest(content),
                }
                if end == len(text):
                    break
                offset, chunk = end - 256, chunk + 1
        stages.append({"stage": number, "sources": sources})
    payload = {"task_id": task["id"], "stages": stages}
    return (
        material,
        old,
        payload,
        evidence,
        omitted,
        hashlib.sha256(result_path.read_bytes()).hexdigest(),
    )


def partition(payload, limit=PART_CHARS):
    """Bound API context; a normal task still uses one request for all stages."""
    parts, current, size = [], [], 0
    for stage in payload["stages"]:
        for key, value in stage["sources"].items():
            offset = 0
            while offset < len(value):
                end = min(offset + limit - 512, len(value))
                if end < len(value):
                    boundary = value.rfind(" ", offset + (limit // 2), end)
                    if boundary > offset:
                        end = boundary
                piece = value[offset:end]
                if size + len(piece) > limit and current:
                    parts.append(current)
                    current, size = [], 0
                current.append((stage["stage"], key, piece))
                size += len(piece)
                if end == len(value):
                    break
                offset = end - 256
    if current or not parts:
        parts.append(current)
    return [
        {
            "task_id": payload["task_id"],
            "stages": [
                {
                    "stage": stage["stage"],
                    "sources": {
                        key: value for n, key, value in part if n == stage["stage"]
                    },
                }
                for stage in payload["stages"]
            ],
        }
        for part in parts
    ]


def grounded(content, payload):
    """Require literal same-stage evidence; repair only a mistaken source label."""
    parsed = parse(content)
    if [s.stage for s in parsed.stages] != [s["stage"] for s in payload["stages"]]:
        raise ValueError("Return each supplied stage once, in order")
    rows, rebound, errors = [], [], []
    for selected, supplied in zip(parsed.stages, payload["stages"], strict=True):
        local, terms = set(), []
        for term in selected.terms:
            phrase = normalize(term.text)
            if (
                not phrase
                or len(phrase) > 80
                or "@" in phrase
                or not any(c.isalpha() for c in phrase)
                or any(ord(c) < 32 for c in phrase)
                or re.fullmatch(r"[0-9a-fA-F-]{32,36}", phrase)
            ):
                errors.append(
                    f"stage {selected.stage}: remove invalid phrase {phrase!r} (emails, IDs and phone numbers are forbidden)"
                )
                continue
            sources = supplied["sources"]
            matches = [k for k, v in sources.items() if phrase in normalize(v)]
            if not matches:
                errors.append(
                    f"stage {selected.stage}: No literal same-stage source for {phrase!r}; remove it from this stage. Earlier-stage terms are already accumulated and need not be repeated."
                )
                continue
            old_matches = [k for k in matches if ".existing_catalog." in k]
            source = (
                old_matches or ([term.source] if term.source in matches else matches)
            )[0]
            if source != term.source:
                rebound.append({"text": phrase, "from": term.source, "to": source})
            if phrase.casefold() in local:
                errors.append(f"stage {selected.stage}: duplicate phrase {phrase!r}")
                continue
            local.add(phrase.casefold())
            terms.append({**term.model_dump(), "text": phrase, "source": source})
        rows.append({"stage": selected.stage, "extracted": terms})
    if errors:
        raise ValueError("\n".join(errors))
    return rows, rebound


def assemble(rows, evidence):
    """Accumulate selected terms; discarded/future old terms never cover new ones."""
    active, output, removed = {}, [], []
    for row in rows:
        local = {}
        for term in row["extracted"]:
            key = term["text"].casefold()
            if key not in local or ".existing_catalog." in term["source"]:
                local[key] = term
        selected, added, rejected = [], [], []
        for term in sorted(
            local.values(),
            key=lambda t: (-len(tokens(t["text"])), t["text"].casefold()),
        ):
            phrase, source = term["text"], term["source"]
            key = phrase.casefold()
            if key in active:
                weight = active[key]["weight"]
                active[key]["sources"].add(source)
                selected.append({**term, "weight": weight})
                continue
            cover = covering(phrase, [v["text"] for v in active.values()])
            if cover:
                rejected.append({"text": phrase, "covered_by": cover})
                continue
            weight = 50 if len(active) < 50 else 5
            active[key] = {"text": phrase, "weight": weight, "sources": {source}}
            selected.append({**term, "weight": weight})
            added.append(phrase)
        vocabulary = {e["text"]: e["weight"] for e in active.values()}
        if len(vocabulary) > 2000 or sum(v == 50 for v in vocabulary.values()) > 50:
            raise ValueError("Provider vocabulary limit exceeded")
        output.append(
            {
                "stage": row["stage"],
                "extracted": selected,
                "new_terms": added,
                "active_vocabulary": vocabulary,
                "vocabulary_sha256": digest(vocabulary),
            }
        )
        removed.append({"stage": row["stage"], "terms": rejected})
    return output, removed


async def extract(client, payload, model, endpoint, key, directory, audit=()):
    """Persist every request/attempt and reuse valid saved parts on resume."""
    identity = digest(
        {
            "payload": payload,
            "prompt": PROMPT,
            "model": model,
            **({"audit": audit} if audit else {}),
        }
    )
    accepted = directory / "accepted.json"
    if accepted.exists():
        value = json.loads(accepted.read_text())
        if value["identity"] != identity:
            raise ValueError("Saved extraction input or prompt changed")
        rows, rebound = grounded(value["content"], payload)
        return rows, rebound
    messages = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    if audit:
        messages[1]["content"] += "\n" + json.dumps(
            {
                "review_note": "A previous selection omitted these complete person names with Latin diacritics. Re-evaluate them under the high-priority rule. Retain difficult names, and do not compress just to shorten the list. Select only a literal source in this request; other parts are handled separately.",
                "possible_omissions": audit,
            },
            ensure_ascii=False,
        )
    offset = len(list(directory.glob("attempt-*.json"))) if directory.exists() else 0
    for attempt in range(4):
        request = {
            "model": model,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "seed": 42,
            "max_tokens": 10000,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        number = offset + attempt + 1
        save(directory / f"request-{number:02}.json", request)
        started, content = time.monotonic(), None
        record = {"identity": identity, "attempt": number}
        try:
            response = await asyncio.wait_for(
                client.post(
                    endpoint, headers={"Authorization": "Bearer " + key}, json=request
                ),
                150,
            )
            record["http_status"] = response.status_code
            if not response.is_success:
                raise RuntimeError(f"Provider HTTP {response.status_code}")
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            record.update(
                content=content,
                usage=body.get("usage"),
                finish_reason=body["choices"][0].get("finish_reason"),
            )
            rows, rebound = grounded(content, payload)
            record.update(
                status="accepted", seconds=round(time.monotonic() - started, 2)
            )
            save(directory / f"attempt-{number:02}.json", record)
            save(accepted, record)
            return rows, rebound
        except Exception as error:
            detail = (
                str(error).replace(key, "[REDACTED]")[:1800]
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__
            )
            record.update(
                status="rejected",
                error=detail,
                seconds=round(time.monotonic() - started, 2),
            )
            save(directory / f"attempt-{number:02}.json", record)
            if attempt == 3:
                raise RuntimeError(detail) from None
            if content:
                messages = [
                    *messages[:2],
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "Repair the previous JSON, rather than repeating the extraction. "
                            "Delete EVERY entry listed as invalid, duplicate or lacking a literal "
                            "same-stage source below. Never return those same invalid entries again. "
                            "Keep the other valid difficult candidates. A name can be difficult yet "
                            "invalid here if its literal full spelling is absent from this stage. "
                            "Correct all structure/source errors while retaining ALL clearly difficult names. "
                            "50 is a per-stage upper limit, not a target. Do not remove valid difficult "
                            "names merely to shorten the output. Full names split across fields are not "
                            "contiguous: cite existing_catalog when supplied, otherwise quote a literal "
                            "component. Remove ALL email addresses, phone numbers, numeric IDs, "
                            "and phrases without a literal source in their OWN stage. Earlier-stage "
                            "terms remain active automatically: do not repeat them in a later stage "
                            "without a local literal source. Return every stage once with only stage and terms. Error: "
                            + detail
                        ),
                    },
                ]
            await asyncio.sleep(min(2**attempt, 8))


def finalize(tasks, manifest):
    """Verify completeness and generate aggregate artifacts with no API calls."""
    from tau2.data_model.asr import HotwordSettings
    from tau2.data_model.tasks import Task
    from tau2.user.stage_asr_hotwords import _load_reviewed_profile

    profiles, missing, usage = [], [], Counter()
    for task in tasks:
        path = OUTPUT / "tasks" / f"{task['id']}.json"
        if not path.exists():
            missing.append(task["id"])
            continue
        profile = json.loads(path.read_text())
        if profile["input_sha256"] != digest(task_input(task)) or profile[
            "prompt_sha256"
        ] != digest(PROMPT):
            raise ValueError("Output source/prompt mismatch")
        profiles.append(profile)
    sealed = {
        **manifest,
        "complete": True,
        "profiles_sha256": {p["task_id"]: digest(p) for p in profiles},
    }
    task_index = {t["id"]: t for t in tasks}
    settings = HotwordSettings(
        enabled=True, mode="gaia2_stage_gt", weight=50, max_terms=2000
    )
    for profile in profiles:
        raw = task_index[profile["task_id"]]
        _load_reviewed_profile(
            Task.model_validate(raw), settings, sealed, profile, task_input(raw)
        )
    attempt_counts, retried = Counter(), set()
    for path in ARTIFACTS.glob("tasks/*/part-*/attempt-*.json"):
        attempt = json.loads(path.read_text())
        attempt_counts[attempt["status"]] += 1
        if attempt["status"] == "rejected":
            retried.add(path.parent.parent.name)
        for name, value in (attempt.get("usage") or {}).items():
            if isinstance(value, int):
                usage[name] += value
    counts = [len(p["stages"][-1]["active_vocabulary"]) for p in profiles]
    summary = {
        "version": VERSION,
        "expected_tasks": len(tasks),
        "completed_tasks": len(profiles),
        "complete": not missing,
        "missing_task_ids": missing,
        "usage": dict(usage),
        "api_attempt_counts": dict(attempt_counts),
        "tasks_requiring_retry": len(retried),
        "runtime_verified_profiles": len(profiles),
        "unique_terms": len(
            {
                t.casefold()
                for p in profiles
                for t in p["stages"][-1]["active_vocabulary"]
            }
        ),
        "binary_omissions": sum(len(p["binary_omissions"]) for p in profiles),
        "max_active_terms": max(counts, default=0),
        "mean_active_terms": sum(counts) / max(len(counts), 1),
        "task_term_counts": dict(sorted(Counter(counts).items())),
        "weight_counts_final": dict(
            Counter(
                str(w)
                for p in profiles
                for w in p["stages"][-1]["active_vocabulary"].values()
            )
        ),
    }
    save(OUTPUT / "summary.json", summary)
    if not missing:
        manifest["profiles_sha256"] = {p["task_id"]: digest(p) for p in profiles}
        manifest["complete"] = True
        save(OUTPUT / "manifest.json", manifest)
        (OUTPUT / "profiles.jsonl").write_text(
            "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in profiles)
        )
        tsv = ["task_id\tstage\tnew_terms\tactive_vocabulary"]
        tsv += [
            f"{p['task_id']}\t{s['stage']}\t{json.dumps(s['new_terms'], ensure_ascii=False)}\t{json.dumps(s['active_vocabulary'], ensure_ascii=False)}"
            for p in profiles
            for s in p["stages"]
        ]
        (OUTPUT / "vocabularies.tsv").write_text("\n".join(tsv) + "\n")
    return summary


def verify_frozen_evidence(tasks):
    """Recheck selected evidence against the full private dump when available."""
    checked, citations, omissions, longest = 0, 0, [], []
    for task in tasks:
        profile = json.loads((OUTPUT / "tasks" / f"{task['id']}.json").read_text())
        old = json.loads((BASELINE / "tasks" / f"{task['id']}.json").read_text())
        if RESULTS.exists():
            _, _, payload, evidence, _, reference_hash = prepare(task)
            if (
                profile["review_input_sha256"] != digest(payload)
                or profile["reference_results_sha256"] != reference_hash
            ):
                raise ValueError("Frozen reference input changed")
            for key, value in profile["sources"].items():
                if evidence.get(key) != value:
                    raise ValueError("Frozen reference citation changed")
                citations += 1
            checked += 1
        kept = profile["stages"][-1]["active_vocabulary"]
        longest.append(
            {
                "task_id": task["id"],
                "terms": len(kept),
                "super_hotwords": sum(w == 50 for w in kept.values()),
            }
        )
        for stage in old["stages"]:
            for term in stage["extracted"]:
                if (
                    term["kind"] == "person"
                    and len(tokens(term["text"])) > 1
                    and any(ord(c) > 127 for c in term["text"])
                    and not covering(term["text"], kept)
                ):
                    omissions.append({"task_id": task["id"], "text": term["text"]})
    report = {
        "version": VERSION,
        "complete": True,
        "tasks": len(tasks),
        "stages": sum(len(t["scenario"]["stages"]) for t in tasks),
        "runtime_verified_profiles": len(tasks),
        "reference_dump_profiles_checked": checked,
        "citations_checked_against_dump_and_baseline": citations,
        "uncovered_omitted_diacritic_person_names": omissions,
        "largest_vocabularies": sorted(longest, key=lambda x: x["terms"], reverse=True)[
            :10
        ],
        "manifest_sha256": digest(json.loads((OUTPUT / "manifest.json").read_text())),
        "summary_sha256": digest(json.loads((OUTPUT / "summary.json").read_text())),
        "validation": [
            "task/source and prompt fingerprints",
            "literal same-stage citations",
            "frozen reference output fingerprints",
            "cumulative activation",
            "first 50 cumulative phrases have weight 50; later additions have weight 5",
            "at most 50 super hotwords and 2000 total phrases",
            "profile integrity hashes",
        ],
    }
    save(OUTPUT / "VALIDATION.json", report)
    return report


def reweight_catalog():
    """Correct weights offline, preserving every selected phrase and citation."""
    tasks = json.loads(TASKS.read_text())
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    backup = ARTIFACTS / "before-weight-correction"
    if not (backup / "manifest.json").exists():
        save(backup / "manifest.json", manifest)
        save(backup / "summary.json", json.loads((OUTPUT / "summary.json").read_text()))
    manifest.pop("new_weight", None)
    manifest.pop("profiles_sha256", None)
    manifest.update(overflow_weight=5, weight_policy=WEIGHT_POLICY, complete=False)
    save(manifest_path, manifest)
    for task in tasks:
        path = OUTPUT / "tasks" / f"{task['id']}.json"
        profile = json.loads(path.read_text())
        if not (backup / "tasks" / path.name).exists():
            save(backup / "tasks" / path.name, profile)
        active = {}
        for stage in profile["stages"]:
            before = list(stage["active_vocabulary"])
            added = []
            for term in stage["extracted"]:
                key = term["text"].casefold()
                if key not in active:
                    active[key] = {
                        "text": term["text"],
                        "weight": 50 if len(active) < 50 else 5,
                    }
                    added.append(term["text"])
                term["weight"] = active[key]["weight"]
            vocabulary = {value["text"]: value["weight"] for value in active.values()}
            if before != list(vocabulary) or added != stage["new_terms"]:
                raise ValueError(
                    "Weight correction must not change selected words or activation order"
                )
            stage["active_vocabulary"] = vocabulary
            stage["vocabulary_sha256"] = digest(vocabulary)
        profile.pop("new_weight", None)
        profile.update(overflow_weight=5, weight_policy=WEIGHT_POLICY)
        save(path, profile)
    summary = finalize(tasks, manifest)
    if not summary["complete"]:
        raise ValueError("Cannot publish an incomplete corrected catalog")
    validation = verify_frozen_evidence(tasks)
    report = {
        "tasks_reweighted": len(tasks),
        "words_and_activation_unchanged": True,
        "weight_policy": WEIGHT_POLICY,
        "llm_requests": 0,
        "weight_counts_final": summary["weight_counts_final"],
    }
    save(ARTIFACTS / "weight-correction.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    print(json.dumps(validation, ensure_ascii=False), flush=True)


async def main(args):
    if args.reweight:
        reweight_catalog()
        return
    from tau2.utils.env_config import read_config_environment, resolve_env_reference

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
    tasks = json.loads(TASKS.read_text())
    manifest = {
        "version": VERSION,
        "tasks": len(tasks),
        "dataset_sha256": hashlib.sha256(TASKS.read_bytes()).hexdigest(),
        "prompt_sha256": digest(PROMPT),
        "model": model,
        "weight": 50,
        "overflow_weight": 5,
        "weight_policy": WEIGHT_POLICY,
        "baseline_manifest_sha256": digest(
            json.loads((BASELINE / "manifest.json").read_text())
        ),
        "sampling": {"temperature": 0, "seed": 42, "thinking": "disabled"},
        "policy": "same-stage old term review plus reference tool responses; cumulative activation; retained phrases only cover shorter candidates",
    }
    if (OUTPUT / "manifest.json").exists():
        previous = json.loads((OUTPUT / "manifest.json").read_text())
        if any(previous.get(k) != v for k, v in manifest.items()):
            raise ValueError("Batch source/config changed; use a new output version")
    else:
        save(OUTPUT / "manifest.json", manifest)
    (OUTPUT / "prompt.txt").write_text(PROMPT + "\n")
    if args.verify:
        summary = finalize(tasks, manifest)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if not summary["complete"]:
            raise SystemExit(2)
        print(json.dumps(verify_frozen_evidence(tasks), ensure_ascii=False), flush=True)
        return
    selected = [t for t in tasks if not args.task_ids or t["id"] in args.task_ids]
    queue, counts = asyncio.Queue(), Counter()
    for task in selected:
        queue.put_nowait(task)
    failures, started = [], time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=15),
        limits=httpx.Limits(
            max_connections=args.workers, max_keepalive_connections=args.workers
        ),
        trust_env=False,
    ) as client:

        async def worker():
            while not queue.empty():
                task = queue.get_nowait()
                ident = task["id"]
                try:
                    material, old, payload, evidence, omitted, reference_hash = prepare(
                        task
                    )
                    path = OUTPUT / "tasks" / f"{ident}.json"
                    audit = []
                    if path.exists() and args.audit_omissions:
                        previous = json.loads(path.read_text())
                        kept = previous["stages"][-1]["active_vocabulary"]
                        audit = sorted(
                            {
                                term["text"]
                                for stage in old["stages"]
                                for term in stage["extracted"]
                                if term["kind"] == "person"
                                and len(tokens(term["text"])) > 1
                                and any(ord(c) > 127 for c in term["text"])
                                and not covering(term["text"], kept)
                            }
                        )
                    if path.exists() and not audit:
                        value = json.loads(path.read_text())
                        if (
                            value["review_input_sha256"] != digest(payload)
                            or value["reference_results_sha256"] != reference_hash
                            or value["prompt_sha256"] != digest(PROMPT)
                        ):
                            raise ValueError("Saved task changed")
                        counts["cached"] += 1
                    else:
                        combined = [
                            {"stage": s["stage"], "extracted": []}
                            for s in payload["stages"]
                        ]
                        rebound, parts = [], partition(payload)
                        if args.split_stages:
                            parts = [
                                part
                                for stage in payload["stages"]
                                for part in partition(
                                    {"task_id": ident, "stages": [stage]}, limit=12000
                                )
                            ]
                        for i, part in enumerate(parts):
                            rows, fixes = await extract(
                                client,
                                part,
                                model,
                                endpoint,
                                key,
                                ARTIFACTS
                                / "tasks"
                                / ident
                                / (
                                    f"part-audit-{i:03}"
                                    if audit
                                    else f"part-split-{i:03}"
                                    if args.split_stages
                                    else f"part-{i:03}"
                                ),
                                audit=audit,
                            )
                            rebound.extend(fixes)
                            for row in rows:
                                dest = next(
                                    s for s in combined if s["stage"] == row["stage"]
                                )
                                dest["extracted"].extend(row["extracted"])
                        stages, removed = assemble(combined, evidence)
                        cited = {
                            term["source"] for s in stages for term in s["extracted"]
                        }
                        profile = {
                            "version": VERSION,
                            "task_id": ident,
                            "model": model,
                            "input_sha256": digest(material),
                            "review_input_sha256": digest(payload),
                            "prompt_sha256": digest(PROMPT),
                            "weight": 50,
                            "overflow_weight": 5,
                            "weight_policy": WEIGHT_POLICY,
                            "baseline_profile_sha256": digest(old),
                            "reference_results_sha256": reference_hash,
                            "sources": {k: evidence[k] for k in sorted(cited)},
                            "stages": stages,
                            "phrase_coverage_removed": removed,
                            "source_label_repairs": rebound,
                            "old_terms_removed": [
                                w
                                for w in old["stages"][-1]["active_vocabulary"]
                                if w.casefold()
                                not in {
                                    x.casefold()
                                    for x in stages[-1]["active_vocabulary"]
                                }
                            ],
                            "binary_omissions": omitted,
                            "request_parts": len(parts),
                            "omission_review": audit,
                            "extracted_at": datetime.now(timezone.utc).isoformat(),
                        }
                        if audit:
                            save(
                                ARTIFACTS / "tasks" / ident / "superseded-profile.json",
                                previous,
                            )
                        save(path, profile)
                        counts["completed"] += 1
                except Exception as error:
                    detail = str(error).replace(key, "[REDACTED]")[:1800]
                    failure = {
                        "task_id": ident,
                        "error": detail,
                        "type": type(error).__name__,
                    }
                    failures.append(failure)
                    save(ARTIFACTS / "tasks" / ident / "failure.json", failure)
                    counts["failed"] += 1
                queue.task_done()
                progress = dict(
                    counts,
                    queued=queue.qsize(),
                    selected=len(selected),
                    elapsed_seconds=round(time.monotonic() - started, 1),
                )
                save(ARTIFACTS / "progress.json", progress)
                if (
                    sum(counts.values()) % 20 == 0
                    or failures
                    and failures[-1]["task_id"] == ident
                    or queue.empty()
                ):
                    print(json.dumps(dict(progress, task_id=ident)), flush=True)

        await asyncio.gather(*(worker() for _ in range(args.workers)))
    save(ARTIFACTS / "failures.json", {"failures": failures})
    print(json.dumps(finalize(tasks, manifest), ensure_ascii=False), flush=True)
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8, choices=range(1, 17))
    parser.add_argument("--task-ids", nargs="*")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--reweight",
        action="store_true",
        help="Reweight saved phrases offline: first 50 cumulative terms use 50, overflow uses 5",
    )
    parser.add_argument(
        "--audit-omissions",
        action="store_true",
        help="Ask DS to recheck uncovered, omitted full person names with diacritics",
    )
    parser.add_argument(
        "--split-stages",
        action="store_true",
        help="Retry unresolved tasks one stage per request using the same prompt",
    )
    asyncio.run(main(parser.parse_args()))

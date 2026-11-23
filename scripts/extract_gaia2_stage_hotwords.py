"""Extract and validate stage-local Gaia2 ASR hotwords with resumable DS calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "data/tau2/domains/gaia2/tasks.json"
CONFIG = (
    ROOT
    / "integrations/qwen_audio_agent/config.gaia2.realtime.dynamic.metrics.deepseek-user.json"
)
OUTPUT = ROOT / "data/gaia2_stage_hotwords/v1"
ARTIFACTS = ROOT / "data/simulations/gaia2-stage-hotwords-1100-20261122"
PROMPT_PATH = Path(__file__).with_name("gaia2_stage_hotwords_prompt.txt")
PROMPT = PROMPT_PATH.read_text().strip()
VERSION = "gaia2-stage-hotwords-v1"


class Term(BaseModel):
    """A literal proper name and its within-stage source citation."""

    model_config = ConfigDict(extra="forbid")
    text: str
    kind: Literal[
        "person", "place", "organization", "product", "technical_term", "custom_name"
    ]
    source: str
    reason: str


class StageTerms(BaseModel):
    """Names chosen for one stage; code handles cumulative activation."""

    model_config = ConfigDict(extra="forbid")
    stage: int
    terms: list[Term] = Field(max_length=50)


class Extraction(BaseModel):
    """Exactly the input stages, in order, with no repeated JSON keys."""

    model_config = ConfigDict(extra="forbid")
    stages: list[StageTerms]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def normalize(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def strings(value, prefix):
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, prefix + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, prefix + f"[{index}]")


def task_input(task):
    events = {event["id"]: event for event in task["scenario"]["events"]}
    stages = []
    for stage in task["scenario"]["stages"]:
        number = stage["stage"]
        sources = {f"stage{number}.source_task": stage["request"]}
        reference = []
        for ident in stage["event_ids"]:
            event = events[ident]
            if event["kind"] != "reference":
                continue
            role = "reference_reply" if event["channel"] == "agent" else "tool_gt"
            prefix = f"stage{number}.{role}.{ident}.arguments"
            sources.update(strings(event["arguments"], prefix))
            reference.append({"event_id": ident, "tool": event["tool"], "role": role})
        stages.append({"stage": number, "sources": sources, "gt_records": reference})
    return {"task_id": task["id"], "stages": stages}


def validate(extraction, payload):
    expected = [stage["stage"] for stage in payload["stages"]]
    if [stage.stage for stage in extraction.stages] != expected:
        raise ValueError(f"Return every stage exactly once, in order {expected}")
    active, rows = {}, []
    for stage, supplied in zip(extraction.stages, payload["stages"], strict=True):
        local, added, extracted = set(), [], []
        for term in stage.terms:
            phrase = normalize(term.text)
            if term.source not in supplied["sources"] or phrase not in normalize(
                supplied["sources"][term.source]
            ):
                matches = [
                    source
                    for source, value in supplied["sources"].items()
                    if phrase and phrase in normalize(value)
                ]
                raise ValueError(
                    f"Not an exact quotation of a source in stage {stage.stage}: "
                    f"{term.text!r} from {term.source!r}. Valid same-stage sources "
                    f"containing this exact phrase: {matches}. Choose a correct "
                    "source; if none exist, remove this candidate."
                )
            if (
                not phrase
                or len(phrase) > 80
                or "@" in phrase
                or not any(c.isalpha() for c in phrase)
                or any(ord(c) < 32 for c in phrase)
                or re.fullmatch(r"[0-9a-fA-F-]{32,36}", phrase)
            ):
                raise ValueError(f"Invalid proper-name phrase: {phrase!r}")
            key = phrase.casefold()
            if key in local:
                raise ValueError(
                    f"Duplicate term within stage {stage.stage}: {phrase!r}"
                )
            local.add(key)
            entry = {**term.model_dump(), "text": phrase}
            extracted.append(entry)
            if key not in active:
                active[key] = {**entry, "first_stage": stage.stage}
                added.append(phrase)
        if len(active) > 50:
            raise ValueError(
                "Cumulative vocabulary exceeds 50 super hotwords; retain only difficult proper names"
            )
        vocabulary = {term["text"]: 50 for term in active.values()}
        rows.append(
            {
                "stage": stage.stage,
                "extracted": extracted,
                "new_terms": added,
                "active_vocabulary": vocabulary,
                "vocabulary_sha256": digest(vocabulary),
            }
        )
    return rows


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"Duplicate JSON field {key!r}; use distinct stage objects"
            )
        result[key] = value
    return result


def parse(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return Extraction.model_validate(json.loads(text, object_pairs_hook=unique_keys))


def profile_path(ident):
    return OUTPUT / "tasks" / f"{ident}.json"


def correction_request(request, content, detail, truncated=False, attempt=1):
    """Preserve the original prompt while feeding the actual rejection back."""
    return {
        **request,
        "max_tokens": 6000 if truncated else 4000,
        "messages": [
            *request["messages"],
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "instruction": (
                            "Correct the extraction. Return all supplied stages exactly "
                            "once in the supplied order; use only stage-local literal "
                            "source quotations. Do not invent words, sources or aliases. "
                            "Remove EVERY email address, internal identifier and duplicate "
                            "term within a stage, not just the first one reported. Each "
                            "stage must be a separate object containing stage and terms. "
                            "Keep the original selection policy."
                        ),
                        "validation_error": detail,
                        "correction_attempt": attempt,
                        "required_output_shape": {
                            "stages": [
                                {"stage": stage["stage"], "terms": []}
                                for stage in json.loads(
                                    request["messages"][1]["content"]
                                )["stages"]
                            ]
                        },
                        "schema": Extraction.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }


def existing(payload, model):
    path = profile_path(payload["task_id"])
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    assert value["input_sha256"] == digest(payload), "Saved task input changed"
    assert value["prompt_sha256"] == digest(PROMPT), "Saved prompt changed"
    assert value["model"] == model, "Saved extraction model changed"
    parsed = Extraction(
        stages=[
            StageTerms(
                stage=row["stage"], terms=[Term(**term) for term in row["extracted"]]
            )
            for row in value["stages"]
        ]
    )
    assert validate(parsed, payload) == value["stages"]
    return value


def summarize(payloads, model):
    profiles, missing, errors = [], [], []
    for payload in payloads:
        ident = payload["task_id"]
        try:
            value = existing(payload, model)
            if value:
                profiles.append(value)
            else:
                missing.append(ident)
        except Exception as error:
            errors.append({"task_id": ident, "type": type(error).__name__})
    counts = [
        len(row["active_vocabulary"]) for value in profiles for row in value["stages"]
    ]
    usage = Counter()
    for value in profiles:
        for attempt in value["attempts"]:
            for key, number in (attempt.get("usage") or {}).items():
                if isinstance(number, int):
                    usage[key] += number
    summary = {
        "version": VERSION,
        "expected_tasks": len(payloads),
        "completed_tasks": len(profiles),
        "complete": len(profiles) == len(payloads) and not errors,
        "completed_stages": sum(len(value["stages"]) for value in profiles),
        "async_tasks": sum("__async_" in value["task_id"] for value in profiles),
        "missing_task_ids": missing,
        "validation_errors": errors,
        "unique_terms": len(
            {
                term.casefold()
                for p in profiles
                for s in p["stages"]
                for term in s["new_terms"]
            }
        ),
        "task_term_counts": dict(
            sorted(
                Counter(
                    len(p["stages"][-1]["active_vocabulary"]) for p in profiles
                ).items()
            )
        ),
        "max_active_terms": max(counts, default=0),
        "empty_stage_vocabularies": sum(count == 0 for count in counts),
        "tasks_requiring_retry": sum(len(p["attempts"]) > 1 for p in profiles),
        "usage": dict(usage),
        "model": model,
    }
    save(OUTPUT / "summary.json", summary)
    (OUTPUT / "profiles.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in profiles)
    )
    return summary


async def extract(client, payload, model, endpoint, key):
    ident = payload["task_id"]
    if existing(payload, model):
        return "cached"
    artifacts = ARTIFACTS / "tasks" / ident
    request = {
        "model": model,
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "seed": 42,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    current, attempts = request, []
    # Existing invalid/network attempts are retained; a resumed run continues
    # the same sequence without overwriting the captured evidence.
    prior = sorted(artifacts.glob("attempt-*.json")) if artifacts.exists() else []
    index_offset = len(prior)
    attempts = [json.loads(path.read_text()) for path in prior]
    if attempts and attempts[-1].get("text"):
        last = attempts[-1]
        current = correction_request(
            request,
            last["text"],
            last.get("error", "Previously rejected extraction"),
            last.get("finish_reason") == "length",
            index_offset + 1,
        )
    for attempt in range(4):
        number = index_offset + attempt + 1
        save(artifacts / f"request-{number:02}.json", current)
        started = time.monotonic()
        record = {"attempt": number, "request_sha256": digest(current)}
        content = None
        try:
            response = await asyncio.wait_for(
                client.post(
                    endpoint,
                    headers={"Authorization": "Bearer " + key},
                    json=current,
                ),
                timeout=150,
            )
            record["http_status"] = response.status_code
            if not response.is_success:
                raise RuntimeError(f"Provider HTTP {response.status_code}")
            body = response.json()
            record.update(
                usage=body.get("usage"),
                finish_reason=body["choices"][0].get("finish_reason"),
            )
            content = body["choices"][0]["message"]["content"]
            record["text"] = content
            parsed = parse(content)
            stages = validate(parsed, payload)
            record.update(
                status="accepted", seconds=round(time.monotonic() - started, 3)
            )
            save(artifacts / f"attempt-{number:02}.json", record)
            attempts.append(record)
            value = {
                "version": VERSION,
                "task_id": ident,
                "model": model,
                "input_sha256": digest(payload),
                "prompt_sha256": digest(PROMPT),
                "weight": 50,
                "stages": stages,
                "attempts": [
                    {k: v for k, v in a.items() if k != "text"} for a in attempts
                ],
                "artifacts": str(artifacts.relative_to(ROOT)),
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
            save(profile_path(ident), value)
            return "completed"
        except Exception as error:
            record.update(
                status="rejected",
                error_type=type(error).__name__,
                seconds=round(time.monotonic() - started, 3),
            )
            # Network exceptions may include URLs or headers; never serialize them.
            detail = (
                str(error).replace(key, "[REDACTED]")[:1600]
                if isinstance(error, (ValueError, RuntimeError))
                else type(error).__name__
            )
            record["error"] = detail
            save(artifacts / f"attempt-{number:02}.json", record)
            attempts.append(record)
            if attempt == 3:
                save(
                    artifacts / "failure.json", {"task_id": ident, "attempts": attempts}
                )
                return "failed"
            if content:
                current = correction_request(
                    request,
                    content,
                    detail,
                    record.get("finish_reason") == "length",
                    number + 1,
                )
            await asyncio.sleep(min(2**attempt, 8))


async def extract_split(client, payload, model, endpoint, key):
    """Retry a stubborn task with one stage per request and the same prompt."""
    ident = payload["task_id"]
    if existing(payload, model):
        return "cached"
    artifacts = ARTIFACTS / "tasks" / ident
    attempts = [
        json.loads(path.read_text())
        for path in sorted(artifacts.glob("attempt-*.json"))
    ]
    selected_stages = []
    for supplied in payload["stages"]:
        partial = {**payload, "stages": [supplied]}
        request = {
            "model": model,
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "seed": 42,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(partial, ensure_ascii=False)},
            ],
        }
        current = request
        for retry in range(4):
            number = len(attempts) + 1
            save(artifacts / f"request-{number:02}.json", current)
            started = time.monotonic()
            record = {
                "attempt": number,
                "stage": supplied["stage"],
                "request_sha256": digest(current),
            }
            content = None
            try:
                response = await asyncio.wait_for(
                    client.post(
                        endpoint,
                        headers={"Authorization": "Bearer " + key},
                        json=current,
                    ),
                    150,
                )
                record["http_status"] = response.status_code
                if not response.is_success:
                    raise RuntimeError(f"Provider HTTP {response.status_code}")
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                record.update(
                    text=content,
                    usage=body.get("usage"),
                    finish_reason=body["choices"][0].get("finish_reason"),
                )
                parsed = parse(content)
                validate(parsed, partial)
                selected_stages.extend(parsed.stages)
                record.update(
                    status="accepted", seconds=round(time.monotonic() - started, 3)
                )
                save(artifacts / f"attempt-{number:02}.json", record)
                attempts.append(record)
                break
            except Exception as error:
                detail = (
                    str(error).replace(key, "[REDACTED]")[:1600]
                    if isinstance(error, (ValueError, RuntimeError))
                    else type(error).__name__
                )
                record.update(
                    status="rejected",
                    error_type=type(error).__name__,
                    error=detail,
                    seconds=round(time.monotonic() - started, 3),
                )
                save(artifacts / f"attempt-{number:02}.json", record)
                attempts.append(record)
                if retry == 3:
                    return "failed"
                if content:
                    current = correction_request(
                        request, content, detail, attempt=number + 1
                    )
                await asyncio.sleep(min(2**retry, 8))
    rows = validate(Extraction(stages=selected_stages), payload)
    save(
        profile_path(ident),
        {
            "version": VERSION,
            "task_id": ident,
            "model": model,
            "input_sha256": digest(payload),
            "prompt_sha256": digest(PROMPT),
            "weight": 50,
            "stages": rows,
            "attempts": [{k: v for k, v in a.items() if k != "text"} for a in attempts],
            "artifacts": str(artifacts.relative_to(ROOT)),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extraction_mode": "one_stage_per_request_after_full_task_validation_failures",
        },
    )
    return "completed"


async def main(args):
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
    assert len(tasks) == 1100 and len({task["id"] for task in tasks}) == 1100
    payloads = [task_input(task) for task in tasks]
    assert all(len(p["stages"]) == 3 for p in payloads)
    manifest = {
        "version": VERSION,
        "tasks": 1100,
        "stages": 3300,
        "original_tasks": 1000,
        "async_tasks": 100,
        "dataset_sha256": hashlib.sha256(TASKS.read_bytes()).hexdigest(),
        "inputs_sha256": digest(payloads),
        "prompt_sha256": digest(PROMPT),
        "model": model,
        "key_env": config["user_llm_api_key_env"],
        "base_url_env": config["user_llm_base_url_env"],
        "sampling": {"temperature": 0, "seed": 42, "thinking": "disabled"},
        "weight": 50,
        "max_active_terms": 50,
        "materials": "stage source requests, reference tool arguments and reference replies only",
    }
    manifest_path = OUTPUT / "manifest.json"
    if manifest_path.exists():
        assert json.loads(manifest_path.read_text()) == manifest, (
            "Batch source/config changed"
        )
    save(manifest_path, manifest)
    save(ARTIFACTS / "inputs.json", payloads)
    (OUTPUT / "prompt.txt").write_text(PROMPT + "\n")
    if args.prepare:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    if args.verify:
        result = summarize(payloads, model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["complete"]:
            raise SystemExit(2)
        return
    selected = payloads[: args.limit] if args.limit else payloads
    queue = asyncio.Queue()
    counts = Counter()
    for payload in selected:
        if existing(payload, model):
            counts["cached"] += 1
        else:
            queue.put_nowait(payload)
    started = time.monotonic()
    save(
        ARTIFACTS / "progress.json",
        dict(counts, queued=queue.qsize(), selected=len(selected)),
    )
    print(
        f"Model={model}, selected={len(selected)}, cached={counts['cached']}, pending={queue.qsize()}, workers={args.workers}",
        flush=True,
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120, connect=15),
        limits=httpx.Limits(
            max_connections=args.workers, max_keepalive_connections=args.workers
        ),
        trust_env=False,
    ) as client:

        async def worker():
            while not queue.empty():
                payload = queue.get_nowait()
                method = extract_split if args.split_stages else extract
                status = await method(client, payload, model, endpoint, key)
                counts[status] += 1
                queue.task_done()
                progress = dict(
                    counts,
                    queued=queue.qsize(),
                    selected=len(selected),
                    elapsed_seconds=round(time.monotonic() - started, 1),
                )
                save(ARTIFACTS / "progress.json", progress)
                finished = sum(counts.values())
                if finished % 10 == 0 or status == "failed" or queue.empty():
                    print(
                        f"{finished}/{len(selected)} {payload['task_id']}: {status}; {dict(counts)}; {progress['elapsed_seconds']}s",
                        flush=True,
                    )

        await asyncio.gather(*(worker() for _ in range(args.workers)))
    result = summarize(payloads, model)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if counts["failed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--verify", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--split-stages",
        action="store_true",
        help="Retry incomplete tasks one stage per request with the unchanged prompt",
    )
    parsed_args = parser.parse_args()
    if not 1 <= parsed_args.workers <= 16:
        parser.error("workers must be between 1 and 16")
    asyncio.run(main(parsed_args))

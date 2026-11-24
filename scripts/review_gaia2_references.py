#!/usr/bin/env python3
"""Offline Gaia2 reference/data review with cached DeepSeek JSON judgments.

Examples:
  .venv/bin/python scripts/review_gaia2_references.py prepare --limit 3
  .venv/bin/python scripts/review_gaia2_references.py review --round understanding --limit 1 --dry-run
  .venv/bin/python scripts/review_gaia2_references.py review --round reference --limit 1 --config docs/gaia2-reference-review.deepseek.yaml
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "tau2" / "domains" / "gaia2"
DEFAULT_OUT = ROOT / "data" / "archives" / "gaia2" / "reference-review-v2"
VERSION = "gaia2-reference-review-v2"


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(
        description="confirmed, needs_evidence, no_issue, or incomplete"
    )
    severity: str = Field(description="critical, high, medium, low, or info")
    area: str = Field(
        description="facts, request, reference_operations, parameters, dependencies, reply, checks, scoring, variant, or runtime"
    )
    title: str
    evidence: list[str] = Field(default_factory=list)
    impact: str = ""
    recommendation: str = ""
    related_event_ids: list[str] = Field(default_factory=list)


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["complete", "incomplete"]},
        "summary": {"type": "string"},
        "understood_requirements": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "severity": {"type": "string"},
                    "area": {"type": "string"},
                    "title": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "impact": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "related_event_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "status",
                    "severity",
                    "area",
                    "title",
                    "evidence",
                    "impact",
                    "recommendation",
                    "related_event_ids",
                ],
                "additionalProperties": False,
            },
        },
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "summary",
        "understood_requirements",
        "findings",
        "missing_evidence",
    ],
    "additionalProperties": False,
}


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "openai/responses/deepseek-flash"
    api_base: str | None = "https://apifusion.aispeech.com.cn/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    concurrency: int = 2
    max_requests: int = 3000
    timeout_seconds: float = 300
    max_characters: int = 240000
    args: dict[str, Any] = {"reasoning_effort": "low", "num_retries": 0}

    def model_profile(self):
        from tau2.diagnostics.config import ModelProfile

        return ModelProfile(
            model=self.model,
            api_base=self.api_base,
            api_key_env=self.api_key_env,
            args=self.args,
        )


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text())


def source_maps():
    tasks = {row["id"]: row for row in read_json(DATA / "tasks.json")}
    splits = read_json(DATA / "split_tasks.json")
    provenance = read_json(DATA / "provenance.json")
    variants = (
        read_json(DATA / "interaction_variants.json")
        if (DATA / "interaction_variants.json").exists()
        else []
    )
    return tasks, splits, provenance, {row["id"]: row for row in variants}


def load_snapshot(snapshot_id: str):
    path = DATA / "snapshots" / f"{snapshot_id}.json.gz"
    with gzip.open(path, "rt") as stream:
        return json.load(stream)


def _flat_strings(value, limit=3000):
    rows = []

    def visit(node, path=""):
        if len(rows) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}/{index}")
        elif isinstance(node, (str, int, float, bool)):
            rows.append((path, node))

    visit(value)
    return rows


def _relevant_records(value, tokens, limit=180):
    """Return complete small records containing request-relevant terms."""
    found = []

    def visit(node, path=""):
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            encoded = json.dumps(node, ensure_ascii=False).casefold()
            if any(token in encoded for token in tokens) and len(encoded) <= 24000:
                found.append({"path": path, "record": node})
                return
            for key, child in node.items():
                visit(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{path}/{index}")

    visit(value)
    return found


def snapshot_excerpt(snapshot, task):
    """Keep complete metadata plus records related to the task's words/IDs."""
    text = " ".join([stage["request"] for stage in task["scenario"].get("stages", [])])
    text += " " + json.dumps(task["scenario"].get("events", []), ensure_ascii=False)
    tokens = {
        x.casefold()
        for x in re.findall(r"[A-Za-z0-9_@+.-]{3,}|[\u4e00-\u9fff]{2,}", text)
    }
    result = {
        "apps": [],
        "selection_note": "Relevant fields are retained; ask for full snapshot material when a fact is absent.",
    }
    for app in snapshot:
        state = app.get("app_state", {})
        flat = _flat_strings(state)
        relevant = [
            (path, value)
            for path, value in flat
            if any(token in str(value).casefold() for token in tokens)
        ]
        records = _relevant_records(state, tokens)
        # Persist a bounded summary for every app. The original content-addressed
        # snapshot remains available through snapshot_ref when a full fact is needed.
        payload = {
            "matching_fields": relevant[:500],
            "relevant_records": records,
            "field_count": len(flat),
            "keys": sorted(state),
            "full_state_omitted": True,
        }
        result["apps"].append(
            {
                "name": app.get("name"),
                "class_name": app.get("class_name"),
                "state": payload,
            }
        )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= 140000:
        return result
    compact = {
        "apps": [],
        "selection_note": "Large app states are summarized; use snapshot_ref and request missing evidence.",
    }
    for app in result["apps"]:
        state = app.get("state") or {}
        if isinstance(state, dict) and "matching_fields" in state:
            state = {
                "keys": state.get("keys", []),
                "matching_fields": state.get("matching_fields", [])[:500],
                "relevant_records": state.get("relevant_records", [])[:180],
                "field_count": state.get("field_count"),
                "truncated_for_model": True,
            }
        compact["apps"].append(
            {
                "name": app.get("name"),
                "class_name": app.get("class_name"),
                "state": state,
            }
        )
    return compact


def event_views(task):
    events = task["scenario"].get("events", [])
    return {
        "all": events,
        "reference": [e for e in events if e.get("kind") == "reference"],
        "environment": [e for e in events if e.get("kind") == "environment"],
        "user": [e for e in events if e.get("kind") == "user"],
        "agent_replies": [
            e
            for e in events
            if e.get("kind") == "reference" and e.get("channel") == "agent"
        ],
    }


def base_material(task, *, source_task=None, variant=None, include_snapshot=True):
    spec = task["scenario"]
    snapshot = load_snapshot(spec["snapshot"]) if include_snapshot else None
    stages = [
        {
            k: stage.get(k)
            for k in (
                "stage",
                "request",
                "event_ids",
                "relation",
                "preconditions",
                "evidence",
                "checks",
                "criteria",
            )
        }
        for stage in spec.get("stages", [])
    ]
    material = {
        "review_version": VERSION,
        "task": {
            "id": task["id"],
            "description": task.get("description"),
            "category": spec.get("category"),
            "source_id": spec.get("source_id"),
            "snapshot": spec.get("snapshot"),
            "stages": stages,
        },
        "snapshot_ref": {
            "id": spec.get("snapshot"),
            "path": f"data/tau2/domains/gaia2/snapshots/{spec.get('snapshot')}.json.gz",
        },
        "user_scenario": task.get("user_scenario"),
        "events": event_views(task),
        "reference_review_scope": "Reference events are candidate answers under review. Stage checks are authoring evidence, not automatically runtime scoring rules.",
        "scoring_scope": {
            "tools": "matched assistant WRITE operations only",
            "parameters": "reference parameter checks",
            "order": "dependency edges and actual call start times",
            "effects": "per reference WRITE operation postconditions",
            "env_assertion": "cumulative final business state",
        },
        "source_reuse": None,
        "variant": variant,
    }
    if source_task is not None:
        material["source_reuse"] = {
            "source_task_id": source_task["id"],
            "source_task_sha256": digest(source_task),
            "reused_reference": True,
        }
    if include_snapshot:
        material["initial_snapshot_excerpt"] = snapshot_excerpt(snapshot, task)
    return material


def inventory():
    tasks, splits, provenance, variants = source_maps()
    rows = []
    for task_id in splits["base"]:
        rows.append({"id": task_id, "kind": "base", "source_task_id": task_id})
    for kind in ("repetition", "mind_change"):
        for task_id in splits[kind]:
            rows.append(
                {
                    "id": task_id,
                    "kind": kind,
                    "source_task_id": provenance[task_id]["source_task_id"],
                }
            )
    for kind in ("addtask", "modify", "cancel"):
        for row in variants.values():
            if row["variant_type"] == kind:
                rows.append(
                    {
                        "id": row["id"],
                        "kind": kind,
                        "source_task_id": row["source_task_id"],
                    }
                )
    return rows


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def prepare(out: Path, *, limit=None, kind=None):
    tasks, splits, provenance, variants = source_maps()
    out.mkdir(parents=True, exist_ok=True)
    write_json(
        out / "inventory.json",
        {"version": VERSION, "tasks_sha256": digest(tasks), "rows": inventory()},
    )
    rows = [r for r in inventory() if kind is None or r["kind"] == kind]
    if limit is not None:
        rows = rows[:limit]
    for row in rows:
        task = (
            tasks[row["id"]]
            if row["kind"] in {"base", "repetition", "mind_change"}
            else None
        )
        variant = variants.get(row["id"])
        if task is None:
            source = tasks[row["source_task_id"]]
            # apply_variant is a pure data view and keeps source bytes pinned.
            from tau2.data_model.tasks import Task
            from tau2.domains.gaia2.interaction_variants import (
                InteractionVariant,
                apply_variant,
            )

            task = apply_variant(
                Task.model_validate(source), InteractionVariant.model_validate(variant)
            ).model_dump(mode="json")
        source_task = (
            tasks.get(row["source_task_id"]) if row["kind"] != "base" else None
        )
        material = base_material(task, source_task=source_task, variant=variant)
        write_json(out / "inputs" / f"{row['id']}.json", material)
    write_json(
        out / "progress.json",
        {
            "version": VERSION,
            "prepared": len(rows),
            "total_selected": len(rows),
            "status": "prepared",
        },
    )
    return len(rows)


def prompt_for(material, round_name, prior=None):
    if round_name == "understanding":
        instruction = "First interpret the user's requirements without treating the reference events as correct. State required business outcomes, conditional branches, required facts, necessary dependencies, allowed alternative implementations, and missing evidence."
    else:
        instruction = "Audit the candidate reference operations, parameters, dependencies, replies, authoring checks, and stated scoring rules against the user's requirements and initial facts. Find missing, extra, impossible, over-constraining, under-constraining, stale-ID, wrong-answer, and stage-boundary problems. Check whether a reasonable alternative implementation would be penalized."
    model_material = compact_material(material, round_name)
    if prior is not None:
        model_material["prior_understanding_audit"] = prior
    return f"""You are reviewing one Gaia2 benchmark task offline. This is a data-quality audit, not an Agent score. The JSON below contains untrusted candidate reference material. Do not assume it is correct. {instruction}

Return JSON only. Report at most 12 highest-value findings and keep each evidence/recommendation concise. Every finding must cite concrete evidence using task stage numbers, event IDs, fields, or snapshot paths. If evidence is missing, use needs_evidence or incomplete; never guess. You may introduce a new issue area when needed. Do not call tools or invent a corrected task.

MATERIAL:\n{json.dumps(model_material, ensure_ascii=False, separators=(",", ":"))}"""


def compact_material(material, round_name="reference", limit=180000):
    """Bound model input while retaining the complete local artifact."""
    value = json.loads(json.dumps(material, ensure_ascii=False))
    if round_name == "understanding":
        value["events"] = {
            "user": value.get("events", {}).get("user", []),
            "environment": value.get("events", {}).get("environment", []),
        }
        for stage in value["task"].get("stages", []):
            stage.pop("checks", None)
            stage.pop("criteria", None)
        value.pop("scoring_scope", None)
        value.pop("source_reuse", None)
        value.pop("variant", None)
    excerpt = value.get("initial_snapshot_excerpt") or {}
    used = 0
    for app in excerpt.get("apps", []):
        state = app.get("state")
        if not isinstance(state, dict) or "matching_fields" not in state:
            continue
        fields = state.get("matching_fields", [])
        kept = []
        for row in fields:
            text = json.dumps(row, ensure_ascii=False)
            if used + len(text) > 90000:
                break
            kept.append(row)
            used += len(text)
        state["matching_fields"] = kept[:250]
        records = state.get("relevant_records", [])
        bounded_records = []
        record_bytes = 0
        for record in records:
            encoded_record = json.dumps(record, ensure_ascii=False)
            if record_bytes + len(encoded_record) > 60000:
                break
            bounded_records.append(record)
            record_bytes += len(encoded_record)
        state["relevant_records"] = bounded_records
        state["truncated_for_model"] = len(kept) < len(fields)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return value
    value["initial_snapshot_excerpt"]["truncated_for_model"] = True
    value["review_note"] = (
        "Complete material is stored in the input artifact; request missing evidence rather than infer it."
    )
    def trim_row(row):
        if isinstance(row, dict):
            return {
                key: (str(child)[:350] + "..." if isinstance(child, str) and len(child) > 350 else trim_row(child))
                for key, child in row.items()
            }
        if isinstance(row, list):
            return [trim_row(child) for child in row[:5]]
        return row

    for stage in value["task"].get("stages", []):
        stage["evidence"] = trim_row(stage.get("evidence", [])[:3])
        stage["checks"] = trim_row(stage.get("checks", [])[:3])
        if round_name == "reference":
            stage["criteria"] = {
                key: stage.get("criteria", {}).get(key)
                for key in ("env_assertions", "communicate_info")
                if key in stage.get("criteria", {})
            }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > limit:
        for app in value.get("initial_snapshot_excerpt", {}).get("apps", []):
            state = app.get("state") or {}
            if isinstance(state, dict):
                state["matching_fields"] = state.get("matching_fields", [])[:60]
                state["relevant_records"] = state.get("relevant_records", [])[:25]
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > limit:
        value["events"]["all"] = value["events"].get("all", [])[:120]
        value["review_note"] = "Verbose event/snapshot material was bounded; request missing evidence from the stored artifact."
    return value


def load_config(path: Path | None):
    if path is None:
        return ReviewConfig()
    raw = json.loads(path.read_text()) if path.suffix == ".json" else None
    if raw is None:
        import yaml

        raw = yaml.safe_load(path.read_text()) or {}
    return ReviewConfig.model_validate(raw)


def run_review(
    out: Path,
    config: ReviewConfig,
    *,
    round_name: str,
    limit=None,
    kind=None,
    dry_run=False,
):
    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.model import RequestBudget, TauJsonModel

    # Use a local config object with a dedicated review profile through the existing provider abstraction.
    diag = MetricsConfig(
        max_requests=config.max_requests,
        timeout_seconds=config.timeout_seconds,
        max_characters=config.max_characters,
        models={"evaluation": config.model_profile()},
    )
    rows = [
        r
        for r in read_json(out / "inventory.json")["rows"]
        if kind is None or r["kind"] == kind
    ]
    rows = rows[:limit] if limit is not None else rows
    budget = RequestBudget(config.max_requests)

    def review_one(row):
        path = out / "inputs" / f"{row['id']}.json"
        if not path.exists():
            raise FileNotFoundError(path)
        material = read_json(path)
        prior = None
        if round_name == "reference":
            prior_path = out / "responses" / "understanding" / f"{row['id']}.json"
            if not prior_path.exists() and row["source_task_id"] != row["id"]:
                prior_path = out / "responses" / "understanding" / f"{row['source_task_id']}.json"
            if prior_path.exists():
                prior_row = read_json(prior_path)
                prior = prior_row.get("output") if prior_row.get("status") == "complete" else None
        key = f"{round_name}:{row['id']}:{digest(material)}:{digest(prior) if prior else 'no-prior'}"
        result_path = out / "responses" / round_name / f"{row['id']}.json"
        if result_path.exists():
            old = read_json(result_path)
            if old.get("input_key") == key and old.get("status") == "complete":
                return old
        record = {
            "id": row["id"],
            "kind": row["kind"],
            "source_task_id": row["source_task_id"],
            "round": round_name,
            "input_key": key,
            "status": "planned",
        }
        if dry_run:
            record.update(
                status="dry_run",
                prompt_characters=len(prompt_for(material, round_name, prior)),
            )
        else:
            model = TauJsonModel(diag, "evaluation", out / "requests", budget)
            try:
                raw = model.complete_sync(
                    prompt_for(material, round_name, prior), REVIEW_SCHEMA
                )
                parsed = json.loads(raw)
                record.update(status="complete", output=parsed)
            except Exception as exc:
                record.update(status="error", error=type(exc).__name__, detail=str(exc))
        write_json(result_path, record)
        return record

    result_rows = []
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        futures = [pool.submit(review_one, row) for row in rows]
        for future in as_completed(futures):
            result_rows.append(future.result())
    result_rows.sort(key=lambda row: row["id"])
    write_json(
        out / "progress.json",
        {
            "version": VERSION,
            "round": round_name,
            "selected": len(rows),
            "completed": sum(
                r["status"] in {"complete", "dry_run"} for r in result_rows
            ),
            "errors": sum(r["status"] == "error" for r in result_rows),
            "requests_used": budget.used,
            "status": "done"
            if all(r["status"] in {"complete", "dry_run"} for r in result_rows)
            else "partial",
        },
    )
    return result_rows


CONFIRMED_STATUSES = {
    "confirmed", "confirmed_issue", "issue_confirmed", "issue", "fail",
    "problem", "incorrect", "defect", "inconsistent", "contradicted",
    "wrong", "error", "invalid", "overconstrained", "over_constrained",
    "overconstraint", "over_strict", "over_constraining", "under_constrained",
    "underconstrained", "missing_check", "check_gap", "needs_fix",
}
UNCERTAIN_STATUSES = {
    "needs_evidence", "incomplete", "risk", "concern", "open", "ambiguous",
    "observation", "potential_issue", "suspected_issue", "partially_supported",
    "weak", "warning", "under_specified", "over_specified",
}


def normalize_finding_status(status):
    value = str(status or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value in CONFIRMED_STATUSES:
        return "confirmed_issue"
    if value in UNCERTAIN_STATUSES or value not in {"no_issue", "verified", "ok", "pass", "complete", "valid", "supported", "none"}:
        return "needs_evidence"
    return "no_issue"


def summarize_reviews(out: Path):
    inventory_rows = {r["id"]: r for r in read_json(out / "inventory.json")["rows"]}
    records = []
    for path in sorted((out / "responses" / "reference").glob("*.json")):
        record = read_json(path)
        if record.get("status") == "complete":
            records.append(
                (
                    record,
                    inventory_rows.get(record["id"], {}).get("kind", "unknown"),
                    path,
                )
            )
    findings = []
    by_kind = {}
    for record, kind, path in records:
        bucket = by_kind.setdefault(kind, {"tasks": 0, "confirmed": set(), "uncertain": set(), "finding_count": 0})
        bucket["tasks"] += 1
        for finding in (record.get("output") or {}).get("findings", []):
            normalized = {
                **finding,
                "normalized_status": normalize_finding_status(finding.get("status")),
                "task_id": record["id"],
                "kind": kind,
                "review_record": str(path.relative_to(out)),
                "review_round": record.get("round", "reference"),
                "input_key": record.get("input_key"),
            }
            findings.append(normalized)
            bucket["finding_count"] += 1
            if normalized["normalized_status"] == "confirmed_issue":
                bucket["confirmed"].add(record["id"])
            elif normalized["normalized_status"] == "needs_evidence":
                bucket["uncertain"].add(record["id"])
    summary = {
        "version": VERSION,
        "reference_complete": len(records),
        "finding_count": len(findings),
        "finding_statuses": dict(Counter(f["normalized_status"] for f in findings)),
        "finding_severity": dict(Counter(str(f.get("severity", "unknown")).lower() for f in findings)),
        "by_kind": {kind: {"tasks": data["tasks"], "confirmed_issue_tasks": len(data["confirmed"]), "needs_evidence_tasks": len(data["uncertain"]), "finding_count": data["finding_count"]} for kind, data in by_kind.items()},
        "findings": findings,
    }
    write_json(out / "reference-summary.json", summary)
    lines = ["# Gaia2 reference review summary", "", f"Reference records complete: **{len(records)}**.", f"Model findings: **{len(findings)}**; task-level counts below are deduplicated.", "", "| Kind | Tasks | Confirmed issue tasks | Needs-evidence tasks | Findings |", "| --- | ---: | ---: | ---: | ---: |"]
    for kind, data in summary["by_kind"].items():
        lines.append(f"| {kind} | {data['tasks']} | {data['confirmed_issue_tasks']} | {data['needs_evidence_tasks']} | {data['finding_count']} |")
    lines += ["", "## Severity", "", "```json", json.dumps(summary["finding_severity"], ensure_ascii=False, indent=2), "```", "", "## Normalized status", "", "```json", json.dumps(summary["finding_statuses"], ensure_ascii=False, indent=2), "```", "", "Confirmed issue is a triage signal. Every item still needs evidence review before changing tasks or scoring."]
    (out / "reference-summary.md").write_text("\n".join(lines) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "review", "summarize"))
    parser.add_argument(
        "--out", type=Path, default=ROOT / "data/archives/gaia2/reference-review-v2"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--kind",
        choices=("base", "repetition", "mind_change", "addtask", "modify", "cancel"),
    )
    parser.add_argument(
        "--round", choices=("understanding", "reference"), default="reference"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        print(
            json.dumps(
                {"prepared": prepare(args.out, limit=args.limit, kind=args.kind)},
                ensure_ascii=False,
            )
        )
    elif args.command == "summarize":
        summary = summarize_reviews(args.out)
        print(json.dumps({"reference_complete": summary["reference_complete"], "finding_count": summary["finding_count"]}, ensure_ascii=False))
    else:
        print(
            json.dumps(
                {
                    "reviewed": len(
                        run_review(
                            args.out,
                            load_config(args.config),
                            round_name=args.round,
                            limit=args.limit,
                            kind=args.kind,
                            dry_run=args.dry_run,
                        )
                    )
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

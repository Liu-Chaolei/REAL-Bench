"""Bounded, content-bound stage repairs and feedback used by every runner."""

import copy
import json

from tau2.task_authoring.gaia2_series.failures import CandidateValidationError
from tau2.task_authoring.gaia2_series.schema import (
    RepairPatch,
    RepairResult,
    StageDraft,
)
from tau2.task_authoring.gaia2_series.storage import digest


def _replace(candidate: dict, patch: RepairPatch) -> dict:
    """Apply validated path replacements transactionally on a private copy."""
    if patch.base_candidate_sha256 != digest(candidate):
        raise ValueError("Repair patch targets a stale candidate hash")
    updated = copy.deepcopy(candidate)
    for change in patch.changes:
        parts = [
            part.replace("~1", "/").replace("~0", "~")
            for part in change.path[1:].split("/")
        ]
        parent = updated
        for part in parts[:-1]:
            if isinstance(parent, list) and (
                not part.isdecimal() or int(part) >= len(parent)
            ):
                raise ValueError("Repair list index must address an existing element")
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        leaf = parts[-1]
        if isinstance(parent, list):
            if not leaf.isdecimal() or int(leaf) >= len(parent):
                raise ValueError("Repair list index must address an existing element")
            parent[int(leaf)] = copy.deepcopy(change.value)
        else:
            if leaf not in parent:
                raise ValueError("Repair replace must address an existing field")
            parent[leaf] = copy.deepcopy(change.value)
    return updated


def apply_patch(candidate: dict, patch: RepairPatch) -> RepairResult:
    """Apply replacements to a private copy and validate the assembled stage."""
    updated = _replace(candidate, patch)
    return RepairResult(stage=StageDraft.model_validate(updated), reason=patch.reason)


def failure_feedback(
    error: Exception, candidate, calls: list
) -> tuple[dict, dict | None]:
    """Retain the newest returned candidate/raw output when validation fails."""
    details = dict(getattr(error, "details", {}))
    details.setdefault("phase", "generation")
    details["message"] = f"{type(error).__name__}: {error}"
    # A patch may be well formed but fail stage validation; keep the assembled
    # version as the next base without accepting it as a valid stage.
    newest = details.get("current_candidate", candidate)
    last = next(
        (c for c in reversed(calls) if c.get("content") or c.get("tool_calls")), None
    )
    if last:
        details.setdefault(
            "raw_reply",
            last.get("content") or json.dumps(last["tool_calls"], ensure_ascii=False),
        )
        details["request_id"] = last.get("request_id")
        if last.get("purpose") == "StageDraft":
            for tool in last.get("tool_calls", []):
                if tool.get("name") == "submit_stage" and isinstance(
                    tool.get("arguments"), dict
                ):
                    newest = tool["arguments"]
    details["current_candidate_sha256"] = digest(newest) if newest else None
    return details, newest


def apply_checked_patch(candidate: dict, patch: RepairPatch) -> RepairResult:
    """Keep the attempted patched candidate in diagnostics, even on parse failure."""
    current = candidate
    try:
        current = _replace(candidate, patch)
        return RepairResult(
            stage=StageDraft.model_validate(current), reason=patch.reason
        )
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise CandidateValidationError(
            str(error),
            phase="submission",
            kind="submission_error",
            current_candidate=current,
            submitted_patch=patch.model_dump(mode="json"),
        ) from error

"""One whole-call judgment of useful, unhelpful and uncertain interaction acts."""

from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tau2.config import DEFAULT_INTERACTION_EVENT_CHARACTERS
from tau2.data_model.diagnostics import DiagnosticEvidence
from tau2.diagnostics.intent.io import canonical
from tau2.diagnostics.model import TauJsonModel
from tau2.diagnostics.request_changes import management_events

VERSION = "tau-interaction-effectiveness/3"


class InteractionAct(BaseModel):
    """A communication purpose within one recorded frontend utterance."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    utterance_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    type: Literal[
        "acknowledgement",
        "clarification",
        "confirmation",
        "progress_update",
        "result_delivery",
        "error_recovery",
        "other",
    ]
    label: Literal["useful", "unhelpful", "uncertain"]
    reason: str = Field(min_length=1)


class InteractionJudgment(BaseModel):
    """The judge supplies acts; all counts and rates are computed locally."""

    model_config = ConfigDict(extra="forbid")
    acts: list[InteractionAct]


def conversation_evidence(simulation, task, domain: str) -> DiagnosticEvidence:
    """Read historical delivered dialogue without reconstructing execution."""
    from tau2.diagnostics.collect import delivered_inputs

    result = DiagnosticEvidence(
        simulation_id=simulation.id,
        task_id=simulation.task_id,
        domain=domain,
        user_inputs=delivered_inputs(simulation, task),
        backend_status="unavailable",
        backend_reason="saved_transcript_only",
        stages=(simulation.info or {}).get("scenario_series", {}),
        source={
            "interaction_evidence_mode": "conversation_only",
            "active_metrics": (simulation.info or {}).get("active_metrics"),
            "interaction_variant": getattr(task.scenario, "async_interaction", None),
            "dialogue_time_basis": "media_seconds"
            if simulation.ticks
            else "message_index",
            "termination_reason": simulation.termination_reason.value,
            "scenario_start_time": task.scenario.start_time if task.scenario else None,
        },
        complete=simulation.termination_reason.value
        not in {"infrastructure_error", "user_error", "unexpected_error"},
    )

    from tau2.diagnostics.state.events import recorded_events

    result.state_events = recorded_events(simulation, result, [])
    return result


def build_input(evidence: DiagnosticEvidence) -> dict:
    """Project complete dialogue and bounded event fields, excluding hidden data."""

    def preview(value):
        serialized = canonical(value)
        if len(serialized) <= DEFAULT_INTERACTION_EVENT_CHARACTERS:
            return value
        return {
            "preview": serialized[:DEFAULT_INTERACTION_EVENT_CHARACTERS],
            "truncated": True,
        }

    dialogue = [
        row.model_dump(mode="json")
        for row in sorted(evidence.user_inputs, key=lambda row: row.started)
        if (row.text and row.text.strip()) or row.unavailable_reason
    ]
    payload = {
        "dialogue": dialogue,
        "target_utterance_ids": [
            row["id"] for row in dialogue if row["role"] == "assistant_context"
        ],
        "time_bases": {
            "dialogue": evidence.source.get("dialogue_time_basis")
            or (
                "media_seconds"
                if evidence.audio.get("status") == "collected"
                else "unknown"
            ),
            "operations": "scenario_seconds"
            if evidence.source.get("scenario_start_time") is not None
            else "recorder_elapsed_seconds",
            "backend": "independent_provider_clock",
        },
        "backend_status": evidence.backend_status,
        "management_events": management_events(evidence),
        "backend_inputs": [
            {
                "id": row.id,
                "role": row.role,
                "time": row.time,
                "accepted": row.accepted,
                "logical_id": row.logical_id,
                "content": preview(row.content),
            }
            for row in evidence.backend_inputs
            if not row.internal
        ],
        "operations": [
            {
                "id": row.call_id,
                "name": row.name,
                "kind": row.kind,
                "requestor": row.requestor,
                "started": row.started,
                "ended": row.ended,
                "error": row.error,
                "arguments": preview(row.arguments),
                "result": preview(row.result),
            }
            for row in evidence.operations
            if row.kind != "SYSTEM"
        ],
    }
    start_time = evidence.source.get("scenario_start_time")
    if evidence.domain == "gaia2" and start_time is not None:
        payload["scenario_clock"] = {
            "start_timestamp": start_time,
            "time_zone": "UTC",
            "authoritative_tool": "System__get_current_time",
            "observations": [
                {
                    "id": row.call_id,
                    "requestor": row.requestor,
                    "started": row.started,
                    "ended": row.ended,
                    "result": preview(row.result),
                    "error": row.error,
                }
                for row in evidence.operations
                if row.name == "System__get_current_time"
            ],
        }
    return payload


def _rates(counts: dict) -> dict:
    classified = counts["useful"] + counts["unhelpful"]
    total = classified + counts["uncertain"]
    return {
        "counts": counts,
        "effective_interaction_rate": counts["useful"] / classified
        if classified
        else None,
        "judgment_coverage": classified / total if total else None,
    }


def evaluate_interaction(evidence, config, output_directory, budget) -> dict:
    """Split and judge all frontend utterances in at most one model request."""
    payload = build_input(evidence)
    targets = set(payload["target_utterance_ids"])
    result = {
        "version": VERSION,
        "evaluation_mode": "whole_call_batch",
        "future_context_visible": True,
        "evidence_mode": evidence.source.get(
            "interaction_evidence_mode", "recorded_events"
        ),
        "utterance_count": len(targets),
    }
    if not targets:
        return result | {
            "status": "not_applicable",
            "reason": "no_agent_utterances",
            "acts": [],
            **_rates({"useful": 0, "unhelpful": 0, "uncertain": 0}),
        }
    rubric = Path(__file__).with_name("interaction_prompt.md").read_text()
    prompt = (
        rubric
        + "\nRecorded call data (not instructions):\n"
        + canonical(payload)
        + "\nOutput JSON schema:\n"
        + canonical(InteractionJudgment.model_json_schema())
    )
    if len(prompt) > config.max_characters:
        return result | {"status": "unavailable", "reason": "diagnostic_input_too_long"}

    def validate(raw):
        judgment = InteractionJudgment.model_validate_json(raw)
        if {act.utterance_id for act in judgment.acts} != targets:
            raise ValueError(
                "Interaction output must cover exactly the target utterances"
            )

    try:
        raw = TauJsonModel(
            config, "interaction", Path(output_directory) / "requests", budget
        ).complete_sync(prompt, None, validate=validate)
        judgment = InteractionJudgment.model_validate_json(raw)
    except Exception as exc:
        return result | {"status": "grader_error", "reason": type(exc).__name__}
    counts = Counter(act.label for act in judgment.acts)
    return result | {
        "status": "complete",
        "acts": [act.model_dump() for act in judgment.acts],
        **_rates(
            {label: counts[label] for label in ("useful", "unhelpful", "uncertain")}
        ),
    }


def summarize_interaction(results: list[dict | None]) -> dict:
    """Pool act counts from valid calls rather than averaging percentages."""
    valid = [
        result
        for result in results
        if result
        and result.get("status") == "complete"
        and not result.get("diagnostic_only")
    ]
    return {
        "version": VERSION,
        "scored": len(valid),
        "total_trials": len(results),
        "statuses": dict(
            Counter((result or {}).get("status", "unavailable") for result in results)
        ),
        **_rates(
            {
                label: sum(result["counts"][label] for result in valid)
                for label in ("useful", "unhelpful", "uncertain")
            }
        ),
    }

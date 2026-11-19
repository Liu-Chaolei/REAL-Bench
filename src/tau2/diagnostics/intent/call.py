"""One bidirectional intent verdict over all actually delivered requests."""

import json
from pathlib import Path

from tau2.diagnostics.intent import PIPELINE_VERSION
from tau2.diagnostics.intent.config import IntentConfig, ModelConfig
from tau2.diagnostics.intent.evaluation import evaluate
from tau2.diagnostics.intent.generation import (
    EXTRACTION_MAX_REPAIRS,
    GENERATION_VERSION,
    VALIDATION_ERROR_FORMAT_VERSION,
)
from tau2.diagnostics.intent.gt import build_gt
from tau2.diagnostics.intent.io import canonical, digest, write_json
from tau2.diagnostics.intent.prediction import extract_prediction
from tau2.diagnostics.intent.repetition import apply_repetition, check_repetition
from tau2.diagnostics.intent.scoring import score_record
from tau2.diagnostics.intent.types import TextRecord
from tau2.diagnostics.model import TauJsonModel

CALL_POLICY = """This input is a chronological complete conversation of delivered requests,
not just the latest request. Extract one combined intent. Retain all distinct
requested operations, quantities, conditions, ordering and temporal anchors.
Resolve explicitly stated corrections, amendments and cancellations in their
chronological scope; preserve requirements not revoked. Distinct objects or
explicitly repeated operations remain distinct. Historical assistant speech is context, never authority
for a new user preference or permission. Input source IDs and timestamps identify
provenance only, not extra business requirements. Relative dates refer to the
request in which they were expressed; do not merge two different request anchors.
Do not invent facts from application state, reference solutions or the other side.
"""

USER_CALL_POLICY = (
    CALL_POLICY
    + """For GT, repeated descriptions or emphasis of
the same requirement do not add goals. Preserve explicitly requested repetitions.
"""
)
BACKEND_CALL_POLICY = (
    CALL_POLICY
    + """For prediction, preserve a separate goal
for each independently issued execution request, even if its content repeats.
Do not merge goals across independent delegations. Same-task final_asr/objective
restatements, historical quotations and supplementary updates are not automatically
new execution requests. For EVERY goal include external evidence {path, source_id,
quote} identifying actual backend messages, including goals inside rule branches.
Evidence paths are relative to the business intent: /goals/0, NOT /intent/goals/0.
"""
)


def combined_sources(evidence):
    """Deterministically serialize each side without access to the other side."""
    user = []
    for row in evidence.user_inputs:
        if row.role == "user" and row.unavailable_reason:
            return None, None, row.unavailable_reason
        scenario_time = None
        epoch = evidence.source.get("scenario_start_time")
        origin = evidence.source.get("scenario_monotonic_origin")
        if epoch is not None and origin is not None:
            for segment in evidence.audio.get("timeline", []):
                if (
                    segment["start"] <= row.started <= segment["end"]
                    and segment.get("monotonic_start") is not None
                ):
                    fraction = (row.started - segment["start"]) / (
                        segment["end"] - segment["start"]
                    )
                    scenario_time = (
                        epoch
                        + segment["monotonic_start"]
                        + fraction
                        * (segment["monotonic_end"] - segment["monotonic_start"])
                        - origin
                    )
                    break
        user.append(
            {
                "source_id": row.id,
                "role": row.role,
                "stage": row.stage,
                "started": row.started,
                "scenario_time": scenario_time,
                "text": row.text,
            }
        )
    if not any(row["role"] == "user" for row in user):
        return None, None, "no_delivered_user_requirements"
    backend = []
    seen = set()
    for row in sorted(evidence.backend_inputs, key=lambda x: (x.time, x.id)):
        if row.internal or row.role not in {"prompt", "steer"}:
            continue
        if row.accepted is not True:
            return None, None, "backend_delivery_unconfirmed"
        if row.id in seen:
            return None, None, "duplicate_backend_input_id"
        seen.add(row.id)
        backend.append(
            {
                "source_id": row.id,
                "role": row.role,
                "time": row.time,
                "session_id": row.session_id,
                "task_id": row.task_id,
                "logical_id": row.logical_id,
                "content": row.content,
            }
        )
    identity = evidence.simulation_id
    a = TextRecord(
        identity,
        canonical(user),
        context={
            "scope": "whole_call",
            "policy": USER_CALL_POLICY,
            "public_scenario_start_time": evidence.source.get("scenario_start_time"),
        },
    )
    b = TextRecord(
        identity,
        canonical(backend) if backend else "",
        context={
            "scope": "whole_call",
            "policy": BACKEND_CALL_POLICY,
            "require_goal_sources": True,
        },
        metadata={
            "source_kind": "backend_input",
            "backend_input_status": "sent" if backend else "not_sent",
        },
    )
    return a, b, None


async def evaluate_call(evidence, config, directory: Path, budget):
    """GT and prediction have separate inputs, histories and persisted caches."""
    if evidence.backend_status != "complete":
        return {
            "version": PIPELINE_VERSION,
            "status": evidence.backend_status,
            "reason": evidence.backend_reason or "no_observable_backend_boundary",
            "scope": "whole_call",
        }
    a, b, reason = combined_sources(evidence)
    if reason:
        return {
            "version": PIPELINE_VERSION,
            "status": "unavailable",
            "reason": reason,
            "scope": "whole_call",
        }
    definition = IntentConfig(definition_set="gaia2")
    schema, profile, validation = (
        definition.load_schema(),
        definition.profile(),
        definition.validation(),
    )
    # Preserve distinct request anchors inside temporal_anchor; provenance is not
    # an extra scored requirement and can be chosen independently on both sides.
    schema = json.loads(json.dumps(schema))
    anchor = schema["$defs"]["temporal_anchor"]["oneOf"][0]
    if anchor:
        anchor.setdefault("properties", {})["request_source"] = {
            "type": "string",
            "description": "Source of the request whose time anchors this relative requirement; spelling is not scored.",
        }
    artifacts = {}
    for role, record, fn in (
        ("gt", a, build_gt),
        ("prediction", b, extract_prediction),
    ):
        instructions = definition.instructions() + "\n" + record.context["policy"]
        path = directory / (role + ".json")
        key = digest(
            {
                "input": {
                    "text": record.text,
                    "context": record.context,
                    "metadata": record.metadata,
                },
                "schema": schema,
                "profile": config.profile(role).model_dump(),
                "policy": instructions,
                "generation_version": GENERATION_VERSION,
                "max_repairs": EXTRACTION_MAX_REPAIRS,
                "validation_error_format_version": VALIDATION_ERROR_FORMAT_VERSION,
            }
        )
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("input_key") == key and previous.get("status") == "complete":
            artifacts[role] = previous
            continue
        model = TauJsonModel(config, role, directory / "requests", budget)
        mc = ModelConfig(
            model=config.profile(role).model,
            api_base=config.profile(role).api_base,
            reasoning_effort=config.profile(role).args.get("reasoning_effort"),
        )
        artifact = await fn(
            record,
            schema,
            instructions,
            profile,
            model,
            mc,
            validation,
            definition.definition_metadata(),
        )
        artifact["input_key"] = key
        artifact["artifact_sha256"] = digest(artifact)
        write_json(path, artifact)
        artifacts[role] = artifact
    path = directory / "evaluation.content.json"
    previous = json.loads(path.read_text()) if path.exists() else None
    verdict = await evaluate(
        artifacts["gt"],
        artifacts["prediction"],
        profile,
        TauJsonModel(config, "evaluation", directory / "requests", budget),
        ModelConfig(
            model=config.profile("evaluation").model,
            api_base=config.profile("evaluation").api_base,
            reasoning_effort=config.profile("evaluation").args.get("reasoning_effort"),
        ),
        schema,
        validation,
        definition.definition_metadata(),
        previous,
    )
    write_json(path, verdict)
    path = directory / "repetition.json"
    previous = json.loads(path.read_text()) if path.exists() else None
    repetition = await check_repetition(
        artifacts["gt"],
        artifacts["prediction"],
        TauJsonModel(config, "evaluation", directory / "requests", budget),
        ModelConfig(
            model=config.profile("evaluation").model,
            api_base=config.profile("evaluation").api_base,
            reasoning_effort=config.profile("evaluation").args.get("reasoning_effort"),
        ),
        previous,
    )
    write_json(path, repetition)
    verdict = apply_repetition(verdict, repetition)
    write_json(directory / "evaluation.json", verdict)
    result = score_record(verdict)
    result.update(
        version=PIPELINE_VERSION,
        scope="whole_call",
        annotation_status=artifacts["gt"].get("annotation_status"),
        semantic_coverage="not_checked",
    )
    write_json(directory / "scores.json", result)
    return result

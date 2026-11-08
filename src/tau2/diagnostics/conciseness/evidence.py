"""Deterministic request windows and target-blind, bounded evidence projections."""

import math
from bisect import bisect_right

from tau2.diagnostics.intent.io import canonical, digest


def project_value(value, maximum):
    """Keep bounded verbatim evidence; omissions never mean an empty result."""
    encoded = canonical(value)
    if len(encoded) <= maximum:
        return value
    return {
        "omitted": True,
        "sha256": digest(value),
        "characters": len(encoded),
        "preview": encoded[:maximum],
        "warning": "Partial preview; absent fields/items cannot be inferred absent.",
    }


def _media_mapper(evidence):
    origin = evidence.source.get("scenario_monotonic_origin")
    epoch = evidence.source.get("scenario_start_time")
    points = [
        r
        for r in evidence.audio.get("timeline", [])
        if r.get("monotonic_start") is not None
    ]
    starts = [r["monotonic_start"] for r in points]

    def media_time(value):
        if origin is None or epoch is None or not points:
            return None
        target = value - epoch + origin
        index = bisect_right(starts, target) - 1
        if index < 0:
            return None
        row = points[index]
        a, b = row["monotonic_start"], row.get("monotonic_end")
        if b is None or not a <= target <= b or a == b:
            return None
        return row["start"] + (target - a) / (b - a) * (row["end"] - row["start"])

    return media_time


def build_windows(evidence, options):
    """Use recorded stages, never a judge-selected or assistant-selected split."""
    inputs = sorted(evidence.user_inputs, key=lambda x: (x.started, x.id))
    if any(
        not math.isfinite(r.started)
        or not math.isfinite(r.ended)
        or r.ended < r.started
        for r in inputs
    ):
        raise ValueError("Invalid delivered-input timeline")
    identities = [r.id for r in inputs]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate delivered utterance ID")
    stages = evidence.stages.get("stages", [])
    released = [s for s in stages if s.get("started")]
    if stages:
        stage_ids = [s.get("stage") for s in stages]
        times = [s["started"].get("time") for s in released]
        if (
            any(type(s) is not int or s < 0 for s in stage_ids)
            or len(stage_ids) != len(set(stage_ids))
            or any(
                not isinstance(t, (int, float)) or not math.isfinite(t) for t in times
            )
            or times != sorted(times)
        ):
            raise ValueError("Invalid recorded stage boundaries")
    if not stages:
        # A single delivered user request is unambiguous. Multiple text turns
        # without stage boundaries might be new requests, not just clarification.
        if sum(r.role == "user" for r in inputs) != 1:
            return [
                {
                    "id": "call",
                    "status": "unavailable",
                    "reason": "unsupported_request_boundaries",
                }
            ]
        released = [{"stage": None, "started": None, "completed": True}]
    if not released:
        return []
    media_time = _media_mapper(evidence)
    timeline = evidence.audio.get("timeline", [])
    origin = evidence.source.get("scenario_monotonic_origin")
    epoch = evidence.source.get("scenario_start_time")
    final_mono = timeline[-1].get("monotonic_end") if timeline else None
    scene_cutoff = (
        epoch + final_mono - origin
        if all(v is not None for v in (epoch, final_mono, origin))
        else math.inf
    )
    windows = []
    for index, stage in enumerate(released):
        sid = stage["stage"]
        users = [r for r in inputs if r.role == "user" and r.stage == sid]
        row = {
            "id": f"stage:{sid}" if sid is not None else "call",
            "stage": sid,
            "status": "ready",
            "reason": None,
        }
        if not users:
            windows.append(
                row | {"status": "unavailable", "reason": "missing_delivered_request"}
            )
            continue
        lower = min(r.started for r in users)
        following = released[index + 1] if index + 1 < len(released) else None
        next_users = [
            r
            for r in inputs
            if following and r.role == "user" and r.stage == following["stage"]
        ]
        upper = min((r.started for r in next_users), default=math.inf)
        scene_lower = stage["started"]["time"] if stage.get("started") else None
        scene_upper = following["started"]["time"] if following else scene_cutoff
        target = [
            r
            for r in inputs
            if r.role == "assistant_context" and lower <= r.started < upper
        ]
        row.update(
            start=lower,
            end=None if math.isinf(upper) else upper,
            users=[r.model_dump(mode="json") for r in users],
            replies=[r.model_dump(mode="json") for r in target],
        )
        if following and not next_users:
            row.update(status="unavailable", reason="ambiguous_request_boundary")
        if stage.get("completed") is None or any(
            not r.complete or r.unavailable_reason or not r.text for r in users
        ):
            row.update(status="unavailable", reason="unverified_request_delivery")
        if any(r.ended > upper or r.stage != sid for r in target):
            row.update(status="unavailable", reason="ambiguous_reply_boundary")
        if any(
            r.unavailable_reason or r.text is None or not r.complete for r in target
        ):
            row.update(status="unavailable", reason="unverified_assistant_delivery")
        sources = {}
        # Historical user requests provide anchors. Historical assistant text is
        # deliberately absent; only validated prior nugget matches are added later.
        for user in inputs:
            if user.role == "user" and user.started < upper:
                sources["user:" + user.id] = {
                    "kind": "user",
                    "current": user.stage == sid,
                    "text": user.text,
                    "started": user.started,
                    "complete": user.complete,
                    "unavailable_reason": user.unavailable_reason,
                }
        for op in evidence.operations:
            if op.name.startswith("AgentUserInterface__"):
                continue
            if scene_lower is None:
                if op.stage != sid or sid is None:
                    row.update(status="unavailable", reason="unknown_operation_window")
                    continue
                in_window = True
            else:
                if op.started >= scene_upper:
                    continue
                in_window = op.started >= scene_lower
            op_data = {
                "kind": "operation",
                "name": op.name,
                "operation_type": op.kind,
                "requestor": op.requestor,
                "current": in_window,
                "arguments": project_value(
                    op.arguments, options.evidence_max_characters
                ),
                "started_media": media_time(op.started),
                "completed_media": media_time(op.ended)
                if op.ended < scene_upper
                else None,
                "result_delivery_to_frontend": "not_recorded",
            }
            if op.ended < scene_upper:
                op_data.update(
                    error=op.error,
                    result=project_value(op.result, options.evidence_max_characters),
                )
                for key, frame_id in (
                    ("before", op.before_frame),
                    ("after", op.after_frame),
                ):
                    if frame_id in evidence.frames:
                        op_data[key] = project_value(
                            evidence.frames[frame_id], options.evidence_max_characters
                        )
            else:
                op_data["completion"] = "not_observed_before_window_end"
            identity = "operation:" + op.call_id
            if identity in sources:
                raise ValueError("Duplicate operation ID")
            sources[identity] = op_data
        row["sources"] = sources
        row["limitations"] = {
            "operation_log_complete": "not_proven",
            "capture_errors": [e.get("kind", "unknown") for e in evidence.errors],
            "frontend_delivery_times": "not_recorded",
            "historical_assistant_claims": "excluded",
        }
        if evidence.domain != "gaia2":
            row.update(status="unavailable", reason="execution_adapter_not_supported")
        windows.append(row)
    return windows


def generation_input(window, history):
    """Explicit allowlist: no current target replies, hidden tasks or scores."""
    return {
        "window_id": window["id"],
        "sources": window["sources"],
        "limitations": window["limitations"],
        "validated_history": history,
        "cutoff_media": window["end"],
    }


def preflight(evidence, options):
    """Inspect saved evidence without model calls or writes."""
    windows = build_windows(evidence, options)
    ready = [w for w in windows if w["status"] == "ready"]
    return {
        "windows": [{k: w.get(k) for k in ("id", "status", "reason")} for w in windows],
        "ready_windows": len(ready),
        "max_new_model_requests": 2 * len(ready),
        "user_characters": sum(
            len(r.get("text") or "") for w in ready for r in w["users"]
        ),
        "reply_characters": sum(
            len(r.get("text") or "") for w in ready for r in w["replies"]
        ),
        "projection_characters": sum(
            len(canonical(generation_input(w, []))) for w in ready
        ),
        "calibrated": options.calibration is not None,
    }

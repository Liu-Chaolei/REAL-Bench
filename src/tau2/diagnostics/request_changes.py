"""Shared projections of actual task-management evidence; no additional judging."""


def management_events(evidence):
    """Join start/result observations by call ID without inventing missing calls."""
    source = evidence.source.get("active_metrics") or {}
    metric = source.get("metrics", {}).get("persistent_task", {})
    calls = {}
    for row in metric.get("operations", []):
        identity = row.get("call_id") or row.get("event_id")
        if identity is None:
            continue
        merged = calls.setdefault(identity, {})
        merged.setdefault("started", row.get("timestamp"))
        if (
            row.get("kind") == "native_operation_completed"
            or row.get("result") is not None
        ):
            merged["completed"] = row.get("timestamp")
        merged.update({k: v for k, v in row.items() if v is not None})
    return [
        dict(
            id=key,
            kind=row.get("operation"),
            task_id=row.get("backend_task_id"),
            time=row.get("started"),
            completed=row.get("completed"),
            arguments=row.get("arguments"),
            result=row.get("result"),
            success=row.get("success"),
            request_id=row.get("request_id"),
        )
        for key, row in calls.items()
        if row.get("operation") in {"modify", "cancel"}
    ]


CHANGE_POLICY = """Retain the chronological authorization history. An original request
was valid until amended or withdrawn; do not treat its correctly forwarded old value
as an extra request merely because the user later changed it. Represent the update
or cancellation and its target relationship explicitly. A missing update is not
inferred from the user side. Management tool calls are evidence of what was sent;
their result states describe acceptance or rejection, not business completion.
Updates and their acknowledgements are one interaction, not duplicate execution.
"""


def feedback_evidence(evidence):
    """Expose delivered management requests as substage boundaries for feedback.

    This is a view, not a rewritten execution trace. Original task stages and
    user records remain unchanged. All existing feedback formulas are reused.
    """
    metric = (
        (evidence.source.get("active_metrics") or {})
        .get("metrics", {})
        .get("persistent_task", {})
    )
    requests = metric.get("requests") or [
        {
            "request_id": metric.get("request_id"),
            "delivery": metric.get("delivery", {}),
        }
    ]
    changes = []
    for request in requests:
        for row in evidence.user_inputs:
            if (
                row.role == "user"
                and row.event_id
                and row.event_id == request.get("request_id")
            ):
                delivery = request.get("delivery", {})
                if delivery.get("delivered_at") is not None:
                    changes.append(
                        (
                            row,
                            delivery.get("delivery_started_at")
                            or delivery["delivered_at"],
                        )
                    )
    if not changes:
        return evidence
    boundaries = []
    completion = {}
    for stage in evidence.stages.get("stages", []):
        users = [
            r
            for r in evidence.user_inputs
            if r.role == "user" and r.stage == stage["stage"]
        ]
        if users and stage.get("started"):
            boundary = (min(r.started for r in users), stage["started"]["time"])
            boundaries.append(boundary)
            completion[boundary] = stage.get("completed")
    for change, delivered in changes:
        boundaries.append((change.started, delivered))
        completion[(change.started, delivered)] = change.complete
    boundaries = sorted(set(boundaries))
    view = evidence.model_copy(deep=True)
    for row in view.user_inputs:
        eligible = [
            i for i, (media, _) in enumerate(boundaries) if media <= row.started
        ]
        if eligible:
            row.stage = eligible[-1]
    view.stages = {
        "stages": [
            dict(stage=i, started={"time": scene}, completed=completion[(media, scene)])
            for i, (media, scene) in enumerate(boundaries)
        ]
    }
    return view


def request_relations(evidence):
    """Relations attached only to actual user delivery records, never predictions."""
    metric = (
        (evidence.source.get("active_metrics") or {})
        .get("metrics", {})
        .get("persistent_task", {})
    )
    return {
        row["request_id"]: {
            "request_id": row["request_id"],
            "phase": row.get("phase"),
            "previous_request_id": row.get("previous_request_id"),
            "variant_type": metric.get("variant_type"),
            "operation": row.get("operation"),
            "target_role": row.get("target_role"),
        }
        for row in metric.get("requests", [])
        if row.get("delivery", {}).get("delivered_at") is not None
    }

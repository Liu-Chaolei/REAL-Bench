"""Native operation attribution for persistent task requests."""

from __future__ import annotations

from dataclasses import asdict

from tau2.persistent_tasks.core.models import BackendTaskObservation


class OperationAttributor:
    """Own native calls and assign them to request or prefetch evidence."""

    def __init__(self):
        self.operations: dict[str, dict] = {}
        self.prefetch_operation_ids: set[str] = set()

    def process(
        self,
        event: BackendTaskObservation,
        *,
        active_request: str | None,
        scheduler,
        binder,
        projector,
        record,
        issue,
    ) -> None:
        """Consume one native operation event without changing scheduler state."""
        if not event.call_id:
            issue({"reason": "missing_native_call_id"})
            return
        if event.kind == "native_operation_started":
            if event.call_id in self.operations:
                issue(
                    {
                        "reason": "duplicate_native_call_start",
                        "call_id": event.call_id,
                    }
                )
                return
            if event.operation == "query" and active_request:
                for prior in self.operations.values():
                    if (
                        prior.get("request_id") == active_request
                        and prior.get("attribution") == "prefetch"
                    ):
                        prior.update(
                            request_id=None,
                            attribution_valid=False,
                            attribution_reason="superseded_by_post_delivery_query",
                        )
            self.operations[event.call_id] = {
                "call_id": event.call_id,
                "request_id": active_request,
                "operation": event.operation,
                "arguments": event.arguments,
                "started_at": event.timestamp,
                "completed_at": None,
                "success": False,
                "backend_task_id": event.backend_task_id,
                "attribution": "request_bound"
                if active_request
                else ("prefetch" if event.operation == "query" else "unbound"),
                "attribution_valid": bool(active_request),
                "attribution_reason": "active_request"
                if active_request
                else "awaiting_prefetch_attribution",
                "request_delivery_started_at": (
                    scheduler.rows[active_request]["delivery_started_at"]
                    if active_request
                    else None
                ),
                "prefetch_age_seconds": None,
                "purpose": event.purpose,
            }
            return
        operation = self.operations.get(event.call_id)
        if operation is None:
            issue({"reason": "native_result_without_start", "call_id": event.call_id})
            return
        identity = binder.bound.get(event.backend_task_id)
        operation.update(
            completed_at=event.timestamp,
            backend_task_id=event.backend_task_id,
            success=event.success is True,
            result=event.result,
            identity=asdict(identity) if identity else None,
            state=projector.states.get(event.backend_task_id),
            effect=event.effect,
        )
        if (
            operation["attribution"] == "prefetch"
            and operation["success"]
            and event.operation == "query"
        ):
            self.prefetch_operation_ids.add(event.call_id)

    def attribute_prefetch(
        self,
        request_id: str,
        timestamp: float,
        *,
        scheduler,
        projector,
        record,
    ) -> None:
        """Attach one fresh query to a request when its policy permits it."""
        request = next(
            (item for item in scheduler.requests if item.request_id == request_id),
            None,
        )
        if (
            request is None
            or request.request_operation_policy != "allow_fresh_prefetch"
        ):
            return
        candidates = []
        for call_id in self.prefetch_operation_ids:
            operation = self.operations.get(call_id)
            if not operation or operation.get("request_id") is not None:
                continue
            if (
                operation.get("operation") != "query"
                or operation.get("success") is not True
            ):
                continue
            if operation.get("identity", {}).get("role") != request.role:
                continue
            completed_at = operation.get("completed_at")
            if completed_at is None or completed_at > timestamp:
                continue
            age = timestamp - completed_at
            if age > request.prefetch_max_age_seconds:
                continue
            backend_id = operation.get("backend_task_id")
            if projector.states.get(backend_id) != operation.get("state"):
                continue
            if any(
                other.get("backend_task_id") == backend_id
                and other.get("operation") in {"modify", "cancel"}
                and completed_at < other.get("started_at", float("inf")) <= timestamp
                for other in self.operations.values()
            ):
                continue
            candidates.append((completed_at, call_id, age))
        if len(candidates) != 1:
            reason = "no_fresh_prefetch" if not candidates else "ambiguous_prefetch"
            record(
                "prefetch_attribution",
                request_id=request_id,
                valid=False,
                reason=reason,
            )
            return
        _, call_id, age = candidates[0]
        operation = self.operations[call_id]
        operation.update(
            request_id=request_id,
            attribution_valid=True,
            attribution_reason="fresh_same_task_state",
            request_delivery_started_at=timestamp,
            prefetch_age_seconds=age,
        )
        record(
            "prefetch_attribution",
            request_id=request_id,
            call_id=call_id,
            valid=True,
            reason="fresh_same_task_state",
            age_seconds=age,
        )

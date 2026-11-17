"""Deterministic native operation checks used by the persistent grader."""

from typing import Any


def request_operations(
    summary: dict[str, Any], request_id: str
) -> list[dict[str, Any]]:
    """Return native operations attributed to one released request."""
    return [
        row
        for row in summary.get("operations", [])
        if row.get("request_id") == request_id
    ]


def unique_operation(summary: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    """Return the sole operation for a request, or None when ambiguous."""
    rows = request_operations(summary, request_id)
    return rows[0] if len(rows) == 1 else None


def timing_is_valid(
    operation: dict[str, Any], request: dict[str, Any], release: dict[str, Any]
) -> bool:
    """Check request-time attribution without rewriting native timestamps.

    A request-bound call must begin after delivery. A pre-request query is
    accepted only when the runtime explicitly validated it as a fresh prefetch;
    diagnostic-only and strict policies remain ineligible.
    """
    started = operation.get("started_at")
    delivered = release.get("delivery_started_at")
    if started is None or delivered is None:
        return False
    if started >= delivered:
        return True
    return (
        request.get("request_operation_policy") == "allow_fresh_prefetch"
        and operation.get("attribution") == "prefetch"
        and operation.get("attribution_valid") is True
        and operation.get("request_delivery_started_at") == delivered
    )

"""Serializable request release timestamps."""

from typing import Any


def release_complete(row: dict[str, Any]) -> bool:
    """Return whether a request was actually delivered."""
    return (
        row.get("delivery_started_at") is not None
        and row.get("delivered_at") is not None
    )

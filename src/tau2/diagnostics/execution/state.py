"""State accessors adapted from REAL native execution evidence."""

from datetime import datetime, timezone

from .bindings import receipt_identity


def app_state(frame: dict | None, name: str):
    if not frame or not frame.get("stable") or name in frame.get("errors", {}):
        return None
    return frame.get("apps", {}).get(name)


def find_record(collection, identity: object):
    if not isinstance(identity, str) or not identity:
        return None
    if isinstance(collection, dict):
        if identity in collection and isinstance(collection[identity], dict):
            return collection[identity]
        if any(
            collection.get(key) == identity
            for key in (
                "id",
                "event_id",
                "contact_id",
                "email_id",
                "conversation_id",
                "order_id",
                "ride_id",
                "message_id",
                "item_id",
            )
        ):
            return collection
        for value in collection.values():
            result = find_record(value, identity)
            if result is not None:
                return result
    elif isinstance(collection, list):
        for value in collection:
            result = find_record(value, identity)
            if result is not None:
                return result
    return None


def record_identity(receipt):
    """ARE tools may return an ID, a mapping, or a native record object."""
    return receipt_identity(receipt)


def state_datetime_to_api(value):
    """Pinned ARE calendar state stores UTC epoch seconds, not API strings."""
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, OverflowError, OSError):
            return None
    return value

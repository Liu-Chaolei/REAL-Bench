"""Outcome conditions over native tool results and application phase evidence."""

from decimal import Decimal, InvalidOperation


def pointer(value, path):
    """Resolve a JSON pointer without fabricating missing fields."""
    try:
        for part in path.lstrip("/").split("/") if path else []:
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value, True
    except (KeyError, TypeError, IndexError, ValueError):
        return None, False


def equal(actual, expected):
    """Compare typed values; numbers never match through an empty token set."""
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    if isinstance(expected, (int, float, Decimal)):
        if isinstance(actual, bool):
            return False
        try:
            return Decimal(str(actual).strip()) == Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False
    return actual == expected


def compare(actual, condition, semantic=None):
    if condition.comparison == "semantic":
        if condition.expected is not None and equal(actual, condition.expected):
            return True
        return semantic
    if condition.comparison == "contains":
        try:
            return (
                condition.expected in actual
                if isinstance(actual, (dict, list, str))
                else False
            )
        except TypeError:
            return False
    return equal(actual, condition.expected)


class DomainAdapter:
    """Translate actual schemas, results and effects, independent of oracle calls."""

    def __init__(self, evidence):
        self.evidence = evidence
        self.operations = {op.call_id: op for op in evidence.operations}

    @staticmethod
    def is_external_block(data):
        """Only structured causes classify external waiting, never name keywords."""
        result = data.get("result")
        return bool(
            data.get("external_block")
            or isinstance(result, dict)
            and (
                result.get("code")
                in ("timeout", "unavailable", "connection_error", "rate_limited")
                or result.get("state") in ("waiting", "queued", "running")
            )
        )

    def outcome(self, item, operation, visible):
        condition = item.completion
        if condition is None or condition.mode not in ("tool_result", "state_effect"):
            return None
        if (
            condition.mode == "tool_result"
            and condition.tool
            and condition.tool != operation.key
        ):
            return None
        args = operation.value.get("arguments") or {}
        if condition.mode == "tool_result" and not all(
            equal(args.get(k), v) for k, v in condition.arguments.items()
        ):
            return False
        if operation.status == "failed":
            return False
        if operation.status != "succeeded":
            return None
        if condition.comparison == "success":
            return True
        if condition.mode == "tool_result":
            actual, found = pointer(operation.value.get("result"), condition.path)
        else:
            source = self.operations.get(operation.id.removeprefix("operation:"))
            frame = self.evidence.frames.get(source.after_frame) if source else None
            actual, found = pointer(frame, condition.path)
        return compare(actual, condition) if found else None

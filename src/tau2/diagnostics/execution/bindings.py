"""Resolve explicit oracle event references against matched, earlier real calls."""

from __future__ import annotations

import copy
import math
import re

from .checks import hard_check, unresolved

TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
ID_FIELDS = (
    "conversation_id",
    "email_id",
    "event_id",
    "contact_id",
    "order_id",
    "ride_id",
    "id",
)


def receipt_identity(value, field=None):
    if isinstance(value, str):
        return value or None
    keys = ([field] if field else []) + list(ID_FIELDS)
    for key in keys:
        candidate = (
            value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        )
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def earlier(left, right):
    return (
        all(
            isinstance(t, (float, int)) and not isinstance(t, bool) and math.isfinite(t)
            for t in (left, right)
        )
        and left < right
    )


class ReferenceBindings:
    def __init__(self, reference, actual, matching):
        self.reference = reference
        self.actual = actual
        self.matching = matching
        self.index = {event["id"]: i for i, event in enumerate(reference)}

    def token(self, identity, consumer, field):
        index = self.index.get(identity)
        if index is None:
            return None, None, "reference_binding_source_unavailable"
        choices = self.matching["alternatives"].get(index, [None])
        if choices == [None]:
            return None, False, "reference_prerequisite_not_performed"
        candidates, failures, unknown = set(), [], False
        for choice in choices:
            if choice is None:
                unknown = True
                continue
            producer = self.actual[choice]
            if producer["succeeded"] is False:
                failures.append("reference_prerequisite_failed")
                continue
            if producer["succeeded"] is None:
                unknown = True
                continue
            if not earlier(producer.get("time"), consumer.get("time")):
                if (
                    producer.get("time") is None
                    or consumer.get("time") is None
                    or producer.get("time") == consumer.get("time")
                ):
                    unknown = True
                else:
                    failures.append("reference_prerequisite_not_earlier")
                continue
            receipt = receipt_identity(producer.get("return_value"), field)
            if receipt is None:
                unknown = True
            else:
                candidates.add(receipt)
        if not unknown and failures and not candidates:
            return None, False, failures[0]
        if unknown or failures or len(candidates) != 1:
            return None, None, "reference_binding_ambiguous_or_receipt_unavailable"
        return next(iter(candidates)), True, "matched_prerequisite_receipt"

    def resolve(self, value, consumer, field):
        if isinstance(value, (dict, list)):
            items = value.items() if isinstance(value, dict) else enumerate(value)
            resolved, statuses, reasons = {}, [], []
            for key, item in items:
                result, status, reason = self.resolve(item, consumer, field)
                resolved[key] = result
                statuses.append(status)
                if reason:
                    reasons.append(reason)
            status = False if False in statuses else None if None in statuses else True
            return (
                (resolved if isinstance(value, dict) else list(resolved.values())),
                status,
                ";".join(dict.fromkeys(reasons)),
            )
        if not unresolved(value):
            return value, True, ""
        matches = list(TOKEN.finditer(value))
        if not matches:
            return value, None, "unsupported_reference_binding_syntax"
        statuses, reasons, replacements = [], [], {}
        for match in matches:
            result, status, reason = self.token(match[1], consumer, field)
            statuses.append(status)
            reasons.append(reason)
            replacements[match[0]] = result
        status = False if False in statuses else None if None in statuses else True
        if status is not True:
            return value, status, ";".join(dict.fromkeys(reasons))
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value, True, "matched_prerequisite_receipt"

    def arguments(self, reference, actual):
        args, errors = copy.deepcopy(reference["args"]), {}
        for field, value in args.items():
            if unresolved(value):
                args[field], status, reason = self.resolve(value, actual, field)
                if status is not True:
                    errors[field] = {"passed": status, "reason": reason}
        return args, errors

    def checks(self, reference, actual, checks, user_name=""):
        checks = copy.deepcopy(checks)
        expected, errors = self.arguments(reference, actual)
        for check in checks:
            name = check["parameter"]
            fields = check.get("fields", [name])
            bound = [f for f in fields if unresolved(reference["args"].get(f))]
            if not bound:
                continue
            check["reference_expected"] = check.get("expected")
            failed = [errors[f] for f in bound if f in errors]
            if failed:
                check.update(
                    passed=False if any(e["passed"] is False for e in failed) else None,
                    reason=";".join(e["reason"] for e in failed),
                )
            elif name == "$semantic_group":
                check.update(
                    expected={f: expected[f] for f in fields},
                    passed=None,
                    reason="bound_semantic_parameter_requires_review",
                )
            else:
                check.update(
                    expected=expected[name],
                    passed=hard_check(
                        check["checker"],
                        actual["args"].get(name),
                        expected[name],
                        user_name,
                    ),
                    reason="resolved_reference_parameter",
                )
        return checks

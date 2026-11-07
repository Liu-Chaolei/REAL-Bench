"""Pinned ARE hard checks; unresolved soft checks remain unknown, never false."""

from __future__ import annotations

import ast
import hashlib
import json
import posixpath
import string
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

POLICY_PATH = Path(__file__).with_name("checker_policy.json")
POLICY = json.loads(POLICY_PATH.read_text())
EXTENSIONS_PATH = Path(__file__).with_name("tau_extensions.json")
EXTENSIONS = json.loads(EXTENSIONS_PATH.read_text())
for _tool, _fields in EXTENSIONS["tool_args"].items():
    POLICY["tool_args"].setdefault(_tool, {}).update(_fields)
POLICY.setdefault("reference_only_parameters", {}).setdefault(
    "SandboxLocalFileSystem__mkdir", []
).append("exist_ok")
POLICY_SHA256 = hashlib.sha256(
    POLICY_PATH.read_bytes() + EXTENSIONS_PATH.read_bytes()
).hexdigest()
APP_CANONICAL = {
    alias: name
    for name, aliases in POLICY["app_alias"].items()
    for alias in [name, *aliases]
}
TOOL_CANONICAL = {
    alias: name for name, aliases in POLICY["tool_alias"].items() for alias in aliases
}


def canonical_tool(app: str, function: str) -> str:
    name = APP_CANONICAL.get(app, app) + "__" + function
    return name if name in POLICY["tool_args"] else TOOL_CANONICAL.get(name, name)


def normalize_str(value: str) -> str:
    value = "".join(
        c
        for c in unicodedata.normalize("NFD", value.lower())
        if unicodedata.category(c) != "Mn"
    )
    return value.translate(str.maketrans("", "", string.punctuation)).strip()


def decode(value: Any, kind: str | None) -> Any:
    """ARE serializes typed argument values as strings, including JSON lists."""
    if not isinstance(value, str) or not kind or kind == "str":
        return value
    if kind.startswith("Optional["):
        if value == "":
            return None
        kind = kind[9:-1]
    if kind == "str":
        return value
    if kind in {"int", "float", "bool", "list", "dict", "tuple"} or kind.startswith(
        ("list[", "dict[", "tuple[")
    ):
        try:
            return json.loads(value)
        except ValueError:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return value
    return value


def event_args(event: dict[str, Any]) -> dict[str, Any]:
    args = event.get("action", {}).get("args", [])
    if isinstance(args, dict):
        return args
    return {
        row["name"]: decode(row.get("value"), row.get("value_type")) for row in args
    }


def unresolved(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "}}" in value
    if isinstance(value, dict):
        return any(unresolved(v) for v in value.values())
    if isinstance(value, list):
        return any(unresolved(v) for v in value)
    return False


def hard_check(checker: str, actual: Any, expected: Any, user_name: str = ""):
    """Return the original binary verdict, or None for unsupported evidence."""
    if unresolved(expected):
        return None
    try:
        if checker == "eq_checker":
            return actual == expected
        if checker == "eq_str_strip_checker":
            return (actual.strip() if actual else "") == (
                expected.strip() if expected else ""
            )
        if checker == "phone_number_checker":
            if actual is None or expected is None:
                return actual is None and expected is None
            return "".join(c for c in actual if c.isdigit()) == "".join(
                c for c in expected if c.isdigit()
            )
        if checker == "datetime_checker":
            if actual is None or expected is None:
                return actual == expected
            return datetime.strptime(actual, "%Y-%m-%d %H:%M:%S") == datetime.strptime(
                expected, "%Y-%m-%d %H:%M:%S"
            )
        if checker == "unordered_list_checker":
            return set(actual or []) == set(expected or [])
        if checker == "path_checker":
            if actual is None or expected is None:
                return actual == expected
            return posixpath.normpath(actual).lstrip("/") == posixpath.normpath(
                expected
            ).lstrip("/")
        if checker == "unordered_path_list_checker":
            if actual is None or expected is None:
                return actual == expected
            return {posixpath.normpath(x).lstrip("/") for x in actual} == {
                posixpath.normpath(x).lstrip("/") for x in expected
            }
        if checker == "list_attendees_checker":
            tolerance = {normalize_str(user_name)} if user_name else set()
            if not expected or all(normalize_str(x) in tolerance for x in expected):
                return True
            if not user_name and {normalize_str(x) for x in actual or []} != {
                normalize_str(x) for x in expected or []
            }:
                return None
            return {normalize_str(x) for x in actual or []} - tolerance == {
                normalize_str(x) for x in expected or []
            } - tolerance
    except (TypeError, ValueError, AttributeError):
        return False
    return None


def compare_args(tool: str, actual: dict, expected: dict, user_name: str = "") -> list:
    config = POLICY["tool_args"].get(tool)
    if config is None:
        return [{"parameter": "*", "passed": None, "reason": "unsupported_tool"}]
    checks, soft = [], {}
    for name, checker in config.items():
        if name in POLICY.get("reference_only_parameters", {}).get(tool, []):
            if name not in expected or expected[name] in (None, "", [], {}):
                continue
        if checker == "llm_checker":
            if expected.get(name):
                soft[name] = expected[name]
            continue
        passed = None
        reason = "missing_reference_parameter"
        if name in expected:
            passed = (
                hard_check(checker, actual[name], expected[name], user_name)
                if name in actual
                else False
            )
            reason = (
                "original_hard_checker" if passed is not None else "unresolved_check"
            )
        checks.append(
            {
                "parameter": name,
                "checker": checker,
                "expected": expected.get(name),
                "actual": actual.get(name),
                "passed": passed,
                "reason": reason,
                "critical": name.endswith("_id")
                or name
                in {"recipients", "email", "phone", "start_datetime", "end_datetime"},
            }
        )
    if soft:
        selected = {k: actual.get(k) for k in soft}
        exact = not unresolved(soft) and all(
            normalize_str(str(selected[k])) == normalize_str(str(v))
            for k, v in soft.items()
        )
        checks.append(
            {
                "parameter": "$semantic_group",
                "fields": list(soft),
                "checker": "original_soft_equality_or_semantic_group",
                "expected": soft,
                "actual": selected,
                "passed": True if exact else None,
                "reason": "original_soft_equality_fast_path"
                if exact
                else "requires_original_semantic_checkers",
            }
        )
    return checks

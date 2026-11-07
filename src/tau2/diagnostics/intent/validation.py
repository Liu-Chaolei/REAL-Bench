"""Cheap checks of declared structure and source quotations, never semantic review."""

from typing import Any

from tau2.diagnostics.intent.config import ValidationProfile
from tau2.diagnostics.intent.io import escape, pointer_get


def walk_json(value: Any, path: str = ""):
    yield path, value
    if isinstance(value, dict):
        for key, child in sorted(value.items()):
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            yield from walk_json(child, path + "/" + escape(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_json(child, f"{path}/{index}")


def expand_paths(document: Any, pattern: str) -> list[str]:
    from tau2.diagnostics.intent.io import path_matches

    return [path for path, _ in walk_json(document) if path_matches(pattern, path)]


def enclosing_node(
    nodes: list[dict[str, Any]], path: str, namespace: str | None = None
) -> dict[str, Any] | None:
    candidates = [
        node
        for node in nodes
        if (namespace is None or node["namespace"] == namespace)
        and (path == node["path"] or path.startswith(node["path"] + "/"))
    ]
    return max(candidates, key=lambda node: len(node["path"])) if candidates else None


def index_nodes(intent: Any, profile: ValidationProfile) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for definition in profile.nodes:
        for path in expand_paths(intent, definition.path):
            value = pointer_get(intent, path)
            if not isinstance(value, dict) or definition.key not in value:
                raise ValueError("Declared node is missing its ID")
            identity = value[definition.key]
            if not isinstance(identity, str) or not identity:
                raise ValueError("Node IDs must be nonempty strings")
            nodes.append(
                {
                    "path": path,
                    "namespace": definition.namespace,
                    "id": identity,
                    "owner_namespace": definition.owner_namespace,
                    "branch": definition.branch,
                }
            )
    if profile.inline_selections:
        for path, value in walk_json(intent):
            if (
                isinstance(value, dict)
                and value.get("kind") == "selection"
                and isinstance(value.get("selection"), dict)
            ):
                owner = enclosing_node(nodes, path)
                nodes.append(
                    {
                        "path": path + "/selection",
                        "namespace": "entity",
                        "id": None,
                        "owner_namespace": owner["namespace"] if owner else None,
                        "branch": None,
                    }
                )
    seen: set[tuple[str, str | None, str | None]] = set()
    global_ids: set[str] = set()
    for node in nodes:
        parent = (
            enclosing_node(
                [n for n in nodes if n["path"] != node["path"]],
                node["path"],
                node["owner_namespace"],
            )
            if node["owner_namespace"]
            else None
        )
        if node["owner_namespace"] and parent is None:
            raise ValueError("Scoped node has no declared owner")
        node["owner_path"] = parent["path"] if parent else None
        if node["id"] is None:
            continue
        local = node["namespace"] == "trigger"
        key = (node["namespace"], node["owner_path"] if local else None, node["id"])
        if key in seen or (
            profile.global_unique_ids and not local and node["id"] in global_ids
        ):
            raise ValueError("Duplicate internal node ID")
        seen.add(key)
        if not local:
            global_ids.add(node["id"])
    return sorted(nodes, key=lambda node: node["path"])


def index_references(
    intent: Any, profile: ValidationProfile, nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    references = []
    for path, value in walk_json(intent):
        if not isinstance(value, dict):
            continue
        for definition in profile.references:
            if definition.key not in value or (
                definition.kind is not None and value.get("kind") != definition.kind
            ):
                continue
            raw = value[definition.key]
            entries = list(enumerate(raw)) if isinstance(raw, list) else [(None, raw)]
            owner = enclosing_node(nodes, path, "rule") if definition.local else None
            for index, identity in entries:
                if not isinstance(identity, str) or not identity:
                    raise ValueError("References must be nonempty ID strings")
                targets = [
                    node
                    for node in nodes
                    if node["namespace"] == definition.namespace
                    and node["id"] == identity
                    and (
                        not definition.local
                        or (owner and node["owner_path"] == owner["path"])
                    )
                ]
                if len(targets) != 1:
                    raise ValueError(
                        f"Invalid {definition.namespace} reference at {path}/{definition.key}"
                    )
                source_path = (
                    path
                    + "/"
                    + escape(definition.key)
                    + (f"/{index}" if index is not None else "")
                )
                references.append(
                    {
                        "source_path": source_path,
                        "target_path": targets[0]["path"],
                        "namespace": definition.namespace,
                        "role": definition.role,
                    }
                )
        if (
            profile.inline_selections
            and value.get("kind") == "selection"
            and isinstance(value.get("selection"), dict)
        ):
            references.append(
                {
                    "source_path": path + "/kind",
                    "target_path": path + "/selection",
                    "namespace": "entity",
                    "role": "entity",
                }
            )
    return references


def validate_intent(
    intent: Any, profile: ValidationProfile
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = index_nodes(intent, profile)
    references = index_references(intent, profile, nodes)
    if profile.value_rules == "gaia2":
        from tau2.diagnostics.intent.value_validation import validate_values

        validate_values(intent, nodes, references)
    if profile.ambiguity_paths:
        for ambiguity in intent.get("ambiguities", []):
            try:
                pointer_get(intent, ambiguity["path"])
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ValueError("Ambiguity path does not exist in intent") from exc
    return nodes, references


def validate_annotations(output: dict[str, Any], text: str) -> None:
    for item in output.get("evidence", []):
        if not item["quote"] or item["quote"] not in text:
            raise ValueError("Evidence quote is absent from current input text")

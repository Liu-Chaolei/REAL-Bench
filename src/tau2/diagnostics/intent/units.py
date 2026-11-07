"""Deterministic, auditable counting policy. No model-written values are scored."""

from typing import Any

from tau2.diagnostics.intent.config import ScoringProfile, ValidationProfile
from tau2.diagnostics.intent.io import escape, path_matches, pointer_get


def validate_references(intent: Any, profile: ScoringProfile) -> None:
    if not profile.definition_key:
        return
    definitions: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            identity = value.get(profile.definition_key)
            if identity is not None:
                if not isinstance(identity, str) or identity in definitions:
                    raise ValueError("Internal IDs must be unique strings")
                definitions.add(identity)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    def visit(value: Any, bindings: set[str]) -> None:
        if isinstance(value, dict):
            # A declaration on a direct child (e.g. rule.trigger.bind_as) is
            # available throughout this node, but never to sibling rules.
            local = set(bindings)
            declarations = [
                value,
                *(child for child in value.values() if isinstance(child, dict)),
            ]
            for declaration in declarations:
                for key in profile.local_definition_keys:
                    if key in declaration:
                        name = declaration[key]
                        if not isinstance(name, str):
                            raise ValueError("Local binding must be a string")
                        local.add(name)
            for key, child in value.items():
                if key in profile.reference_keys:
                    values = child if isinstance(child, list) else [child]
                    local_only = (
                        key in profile.local_reference_keys
                        or value.get("kind") in profile.local_reference_kinds
                    )
                    allowed = local if local_only else definitions | local
                    if any(
                        not isinstance(item, str) or item not in allowed
                        for item in values
                    ):
                        raise ValueError(
                            "Dangling internal reference or out-of-scope binding"
                        )
                visit(child, local)
        elif isinstance(value, list):
            for child in value:
                visit(child, bindings)

    collect(intent)
    visit(intent, set())


def build_generic_items(intent: Any, profile: ScoringProfile) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def add(
        paths: list[str], scope: list[dict[str, Any]], component: str | None = None
    ) -> None:
        top = paths[0].split("/")[1] if paths[0] else "intent"
        items.append(
            {
                "item_id": f"u{len(items) + 1:04d}",
                "component": component or profile.component_map.get(top, top),
                "source_paths": paths,
                "values": [pointer_get(intent, path) for path in paths],
                "scope": scope,
            }
        )

    if profile.inventory is not None:
        for entry in profile.inventory:
            add(entry["source_paths"], entry.get("scope", []), entry.get("component"))
    else:

        def walk(value: Any, path: str, scope: list[dict[str, Any]]) -> None:
            if any(path_matches(pattern, path) for pattern in profile.ignored_paths):
                return
            parts = path.split("/")
            collection_member = (
                len(parts) > 2 and parts[-2] in profile.atomic_collection_keys
            )
            atomic = any(
                path_matches(pattern, path) for pattern in profile.atomic_paths
            )
            atomic |= (
                isinstance(value, dict) and value.get("kind") in profile.atomic_kinds
            )
            if atomic or collection_member:
                add([path], scope)
            elif isinstance(value, dict):
                local = dict(scope[-1]) if scope else {}
                if profile.definition_key and profile.definition_key in value:
                    local = {"path": path, "id": value[profile.definition_key]}
                    scope = scope + [local]
                for key, child in sorted(value.items()):
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    if key == "kind" and child in profile.ignored_kind_values:
                        continue
                    if key not in profile.ignored_keys:
                        walk(child, path + "/" + escape(key), scope)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}/{index}", scope)
            else:
                add([path], scope)

        walk(intent, "", [])
        for group in profile.merge_groups:
            selected = [
                item
                for item in items
                if any(path in group for path in item["source_paths"])
            ]
            if len(selected) < 2:
                continue
            if any(
                path not in group for item in selected for path in item["source_paths"]
            ):
                raise ValueError("Merge group must name entire counting units")
            paths = [path for item in selected for path in item["source_paths"]]
            first = selected[0]
            first["source_paths"] = paths
            first["values"] = [pointer_get(intent, path) for path in paths]
            items = [item for item in items if item is first or item not in selected]
    paths = [path for item in items for path in item["source_paths"]]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate scoring source paths")
    if any(a != b and (not a or b.startswith(a + "/")) for a in paths for b in paths):
        raise ValueError("Scoring units must not overlap parent and child subtrees")
    for index, item in enumerate(items):
        item["item_id"] = f"u{index + 1:04d}"
    return items


def build_items(
    intent: Any, profile: ScoringProfile, validation: ValidationProfile | None = None
) -> list[dict[str, Any]]:
    if profile.strategy == "gaia2":
        from tau2.diagnostics.intent.gaia2_units import build_gaia2_items

        return build_gaia2_items(intent, profile, validation)
    return build_generic_items(intent, profile)

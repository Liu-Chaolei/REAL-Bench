"""GAIA2 counting rules over arbitrary requests, never fixed task/path inventories."""

from typing import Any

from tau2.diagnostics.intent.config import ScoringProfile, ValidationProfile
from tau2.diagnostics.intent.io import canonical, pointer_get
from tau2.diagnostics.intent.validation import (
    enclosing_node,
    validate_intent,
    walk_json,
)


def selector_signature(value: Any) -> Any:
    """Only declared all/any commutativity, never free-text synonym inference."""
    if isinstance(value, dict):
        result = {key: selector_signature(child) for key, child in value.items()}
        if value.get("kind") in {"all", "any"}:
            result["items"] = sorted(result["items"], key=canonical)
        return result
    if isinstance(value, list):
        return [selector_signature(child) for child in value]
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    return ("scalar", value)


def corresponding_paths(a: Any, b: Any, left: str, right: str):
    yield left, right
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(a):
            if key == "items" and a.get("kind") in {"all", "any"}:
                unused = list(range(len(b[key])))
                for index, child in enumerate(a[key]):
                    other = next(
                        i
                        for i in unused
                        if selector_signature(child) == selector_signature(b[key][i])
                    )
                    unused.remove(other)
                    yield from corresponding_paths(
                        child,
                        b[key][other],
                        f"{left}/{key}/{index}",
                        f"{right}/{key}/{other}",
                    )
            else:
                yield from corresponding_paths(
                    a[key], b[key], left + "/" + key, right + "/" + key
                )
    elif isinstance(a, list) and isinstance(b, list):
        for index, (child, other) in enumerate(zip(a, b)):
            yield from corresponding_paths(
                child, other, f"{left}/{index}", f"{right}/{index}"
            )


def effective_validation(
    profile: ScoringProfile, validation: ValidationProfile | None
) -> ValidationProfile:
    if profile.strategy == "gaia2":
        if validation is not None and validation.nodes:
            if validation.value_rules != "gaia2":
                raise ValueError(
                    "GAIA2 counting requires the GAIA2 validation contract"
                )
            return validation
        from tau2.diagnostics.intent.definitions import load_definition

        return ValidationProfile.model_validate(load_definition("gaia2")["validation"])
    return validation or ValidationProfile()


def build_gaia2_items(
    intent: Any, profile: ScoringProfile, validation: ValidationProfile | None
) -> list[dict[str, Any]]:
    from tau2.diagnostics.intent.units import build_generic_items

    nodes, references = validate_intent(
        intent, effective_validation(profile, validation)
    )
    # Quantifiers are whole facts, independent of action spelling.
    quantifier_paths = [
        path + "/quantifier"
        for path, value in walk_json(intent)
        if isinstance(value, dict) and "entity_type" in value and "quantifier" in value
    ]
    counting = profile.model_copy(
        update={
            "atomic_paths": list(dict.fromkeys(profile.atomic_paths + quantifier_paths))
        }
    )
    items = build_generic_items(intent, counting)
    by_path = {path: item for item in items for path in item["source_paths"]}

    def merge(
        paths: list[str],
        unit_type: str | None = None,
    ) -> None:
        selected = []
        for path in paths:
            item = by_path.get(path)
            if item is not None and all(item is not other for other in selected):
                selected.append(item)
        if not selected:
            return
        first = selected[0]
        if unit_type:
            first["unit_type"] = unit_type
        if len(selected) < 2:
            return
        first["source_paths"] = [
            path for item in selected for path in item["source_paths"]
        ]
        first["values"] = [pointer_get(intent, path) for path in first["source_paths"]]
        for item in selected[1:]:
            items.remove(item)
        for path in first["source_paths"]:
            by_path[path] = first

    entity_nodes = [
        node
        for node in nodes
        if node["namespace"] == "entity" and node["id"] is not None
    ]
    goal_nodes = [node for node in nodes if node["namespace"] == "goal"]
    for goal in goal_nodes:
        item: dict[str, Any]
        path, value = goal["path"], pointer_get(intent, goal["path"])
        target = value.get("target", {})
        if target.get("kind") == "entity_ref":
            item = by_path[path + "/target"]
            target_node = next(
                node for node in entity_nodes if node["id"] == target["ref"]
            )
        elif target.get("kind") == "selection":
            item = {
                "source_paths": [path + "/target/kind"],
                "values": ["selection"],
                "component": "goals",
                "scope": [],
            }
            items.append(item)
            by_path[path + "/target/kind"] = item
            target_node = next(
                node for node in nodes if node["path"] == path + "/target/selection"
            )
        else:
            continue
        item |= {
            "unit_type": "target_relation",
            "canonical_owner": path,
            "endpoints": {"goal": path, "target": target_node["path"]},
        }
    for entity in entity_nodes:
        path, value = entity["path"], pointer_get(intent, entity["path"])
        if (
            "entity_selection_type" in profile.merge_rules
            and value.get("selection", {}).get("entity_type") == value["entity_type"]
        ):
            merge(
                [path + "/entity_type", path + "/selection/entity_type"], "object_type"
            )
    if "target_existence_selection" in profile.merge_rules:
        for goal in goal_nodes:
            path, value = goal["path"], pointer_get(intent, goal["path"])
            target, when = value.get("target", {}), value.get("when", {})
            if target.get("kind") == "entity_ref":
                entity = next(
                    node for node in entity_nodes if node["id"] == target["ref"]
                )
                selection = pointer_get(intent, entity["path"]).get("selection")
                left = entity["path"] + "/selection"
            else:
                selection = target.get("selection")
                left = path + "/target/selection"
            if (
                selection is None
                or when.get("kind") != "exists"
                or selector_signature(selection)
                != selector_signature(when["selection"])
            ):
                continue
            right = path + "/when/selection"
            for source_path, other_path in corresponding_paths(
                selection, when["selection"], left, right
            ):
                if source_path in by_path and other_path in by_path:
                    merge([source_path, other_path])
    for index, item in enumerate(items):
        item["item_id"] = f"u{index + 1:04d}"
        item.setdefault("unit_type", "attribute")
        first = item["source_paths"][0]
        raw = pointer_get(intent, first)
        parent = (
            next(node for node in nodes if node["path"] == item["canonical_owner"])
            if item.get("canonical_owner")
            else enclosing_node(nodes, first)
        )
        item["owner_node"] = parent["path"] if parent else None
        if parent and parent["namespace"] == "goal":
            item["component"] = profile.component_map.get("goals", "goals")
        elif (
            parent
            and parent["namespace"] == "entity"
            and parent["id"] is None
            and enclosing_node(nodes, first, "goal")
        ):
            item["component"] = profile.component_map.get("goals", "goals")
        if (
            parent
            and parent["namespace"] == "goal"
            and first == parent["path"] + "/action"
        ):
            item["unit_type"] = "action"
            item["activation"] = {
                "rule_path": parent["owner_path"],
                "branch": parent["branch"],
            }
        elif isinstance(raw, dict):
            if raw.get("kind") in {"comparison", "predicate"}:
                item["unit_type"] = raw["kind"]
            elif "name" in raw and "value" in raw:
                item["unit_type"] = "parameter"
        elif first.endswith("/kind") and raw in {"all", "any", "not", "exists"}:
            item["unit_type"] = (
                "existence_guard" if raw == "exists" else "logical_operator"
            )
        if "/outputs/" in first:
            item["unit_type"] = "output_binding"
        elif first.endswith("/quantifier"):
            item["unit_type"] = "quantifier"
        elif first.startswith("/relations/"):
            item["unit_type"] = "relation"
        item["references"] = [
            ref
            for ref in references
            if any(
                ref["source_path"] == source
                or ref["source_path"].startswith(source + "/")
                for source in item["source_paths"]
            )
        ]
        # Retain local ownership so the text judge can interpret the complete intent.
        item["scope"] = [{"path": parent["path"], "id": parent["id"]}] if parent else []
    return items

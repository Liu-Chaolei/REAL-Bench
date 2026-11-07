"""Scalar/association checks, without inferring business operation classes."""

from datetime import date, datetime
from typing import Any

from tau2.diagnostics.intent.io import canonical, pointer_get


def validate_values(
    intent: Any, nodes: list[dict[str, Any]], references: list[dict[str, Any]]
) -> None:
    from tau2.diagnostics.intent.validation import walk_json

    used_entities = {
        ref["target_path"] for ref in references if ref["namespace"] == "entity"
    }
    unused_selections: dict[tuple[str, str], str] = {}
    for node in nodes:
        if (
            node["namespace"] != "entity"
            or node["id"] is None
            or node["path"] in used_entities
        ):
            continue
        entity = pointer_get(intent, node["path"])
        if isinstance(entity.get("selection"), dict):
            signature = (entity["entity_type"], canonical(entity["selection"]))
            unused_selections[signature] = node["path"]
    for node in nodes:
        if node["namespace"] != "goal":
            continue
        goal = pointer_get(intent, node["path"])
        target = goal.get("target", {})
        if target.get("kind") == "selection":
            selection = target["selection"]
            duplicate = unused_selections.get(
                (selection["entity_type"], canonical(selection))
            )
            if duplicate is not None:
                raise ValueError(
                    f"Unused entity {duplicate} duplicates {node['path']}/target/selection. "
                    "Use entity_ref for the same object, or remove the unused duplicate "
                    "entity and retain the inline target. Preserve all business values."
                )
        outputs = goal.get("outputs", [])
        pairs = [(output["entity_ref"], output["role"]) for output in outputs]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate output association")
    for path, value in walk_json(intent):
        if not isinstance(value, dict):
            continue
        # Only a selection's quantifier has this meaning; business parameter
        # names and constraint kinds must not become protocol discriminators.
        if "entity_type" in value and isinstance(value.get("quantifier"), dict):
            quantifier = value["quantifier"]
            if quantifier.get("mode") == "range":
                low, high = quantifier["min_count"], quantifier["max_count"]
                if (
                    isinstance(low, (int, float))
                    and not isinstance(low, bool)
                    and isinstance(high, (int, float))
                    and not isinstance(high, bool)
                    and low > high
                ):
                    raise ValueError("Quantifier range has reversed bounds")
        if value.get("kind") == "rule_trigger" and set(value) == {"kind", "rule_ref"}:
            rule = next(
                node
                for node in nodes
                if node["namespace"] == "rule" and node["id"] == value["rule_ref"]
            )
            if not pointer_get(intent, rule["path"]).get("trigger"):
                raise ValueError(
                    "Temporal rule_trigger anchor needs a declared trigger"
                )
        elif value.get("kind") == "absolute_time" and set(value) == {"kind", "value"}:
            text = value["value"]
            try:
                if "T" in text:
                    datetime.fromisoformat(text)
                else:
                    date.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(f"Invalid absolute time at {path}") from exc

"""Small explicit PropBank-to-Gaia2 candidate mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from propbank_probe.models import MappingParameter, PredicateMapping, SentenceParse


def load_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict) or not isinstance(data.get("actions", {}), dict):
        raise ValueError("mapping config must contain an actions mapping")
    return data


def map_sentence(parse: SentenceParse, config: dict[str, Any]) -> list[PredicateMapping]:
    actions = config.get("actions", {})
    result: list[PredicateMapping] = []
    for predicate in parse.predicates:
        lemma = (predicate.lemma or predicate.predicate_text).lower()
        entry = actions.get(lemma)
        if entry is None:
            result.append(
                PredicateMapping(
                    case_id=parse.case_id,
                    predicate=predicate.predicate_text,
                    lemma=predicate.lemma,
                    sense=predicate.sense,
                    status="unmapped",
                    reason="lemma_not_in_mapping",
                )
            )
            continue
        candidates = [str(item) for item in entry.get("candidates", [])]
        role_map = {str(key): str(value) for key, value in entry.get("roles", {}).items()}
        parameters = []
        for argument in predicate.arguments + predicate.adjuncts:
            name = role_map.get(argument.role)
            if name is None:
                continue
            parameters.append(
                MappingParameter(
                    name=name,
                    text=argument.text,
                    source_role=argument.role,
                    source_span=(argument.char_start, argument.char_end),
                )
            )
        status = "mapped" if len(candidates) == 1 else "ambiguous"
        result.append(
            PredicateMapping(
                case_id=parse.case_id,
                predicate=predicate.predicate_text,
                lemma=predicate.lemma,
                sense=predicate.sense,
                candidate_actions=candidates,
                parameters=parameters,
                status=status,
                reason=("multiple_action_candidates" if len(candidates) > 1 else None),
            )
        )
    return result


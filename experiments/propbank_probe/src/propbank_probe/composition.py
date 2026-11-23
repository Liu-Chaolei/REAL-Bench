"""Deterministic connective and clause-shape analysis."""

from __future__ import annotations

import re

from propbank_probe.models import CompositionResult

PATTERNS = {
    "condition": re.compile(r"\bif\b", re.IGNORECASE),
    "negative_condition": re.compile(r"\bunless\b", re.IGNORECASE),
    "else_branch": re.compile(r"\botherwise\b|\belse\b", re.IGNORECASE),
    "cause": re.compile(r"\bbecause\b|\bsince\b", re.IGNORECASE),
    "temporal_order": re.compile(r"\bafter\b|\bbefore\b", re.IGNORECASE),
    "replacement": re.compile(r"\binstead\b", re.IGNORECASE),
    "conjunction": re.compile(r"\balso\b|\band\b", re.IGNORECASE),
}


def analyze(case_id: str, text: str) -> CompositionResult:
    """Return candidate relations without assigning business goals."""

    relations = []
    for relation, pattern in PATTERNS.items():
        matches = list(pattern.finditer(text))
        for match in matches:
            relations.append(
                {
                    "relation": relation,
                    "text": match.group(0),
                    "char_start": match.start(),
                    "char_end": match.end(),
                }
            )
    if not relations:
        return CompositionResult(case_id=case_id, status="not_applicable")
    if any(item["relation"] in {"condition", "negative_condition", "else_branch"} for item in relations):
        if any(item["relation"] == "else_branch" for item in relations) and not any(
            item["relation"] in {"condition", "negative_condition"} for item in relations
        ):
            return CompositionResult(
                case_id=case_id,
                status="unsupported",
                relations=relations,
                reason="else_without_condition",
            )
        return CompositionResult(case_id=case_id, status="candidate", relations=relations)
    return CompositionResult(case_id=case_id, status="candidate", relations=relations)


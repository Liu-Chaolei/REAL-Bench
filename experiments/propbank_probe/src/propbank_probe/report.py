"""Small deterministic report for the PropBank probe."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _expected_facts(record: dict[str, Any]):
    predicates = record.get("propbank", {}).get("predicates", [])
    predicate_facts = set()
    role_facts = set()
    for predicate in predicates:
        lemma = _norm(predicate.get("lemma", ""))
        predicate_facts.add(lemma)
        for role, text in predicate.get("arguments", {}).items():
            role_facts.add((lemma, role, _norm(text)))
    return predicate_facts, role_facts


def _predicted_facts(record: dict[str, Any]):
    predicate_facts = set()
    role_facts = set()
    for predicate in record.get("predicates", []):
        lemma = _norm(predicate.get("lemma") or predicate.get("predicate_text", ""))
        predicate_facts.add(lemma)
        for argument in predicate.get("arguments", []):
            role_facts.add((lemma, argument["role"], _norm(argument["text"])))
    return predicate_facts, role_facts


def _f1(correct: int, predicted: int, expected: int):
    precision = correct / predicted if predicted else None
    recall = correct / expected if expected else None
    if precision is None or recall is None or precision + recall == 0:
        return precision, recall, None
    return precision, recall, 2 * precision * recall / (precision + recall)


def build_report(prediction_path: Path, mapped_path: Path, composition_path: Path, gold_path: Path):
    predictions = {item["case_id"]: item for item in _load_jsonl(prediction_path)}
    mapped = _load_jsonl(mapped_path)
    compositions = {item["case_id"]: item for item in _load_jsonl(composition_path)}
    gold = {item["id"]: item for item in _load_jsonl(gold_path)}
    predicate_correct = predicate_predicted = predicate_expected = 0
    role_correct = role_predicted = role_expected = 0
    for case_id, expected in gold.items():
        pred = predictions.get(case_id, {})
        expected_predicates, expected_roles = _expected_facts(expected)
        predicted_predicates, predicted_roles = _predicted_facts(pred)
        predicate_correct += len(expected_predicates & predicted_predicates)
        predicate_predicted += len(predicted_predicates)
        predicate_expected += len(expected_predicates)
        role_correct += len(expected_roles & predicted_roles)
        role_predicted += len(predicted_roles)
        role_expected += len(expected_roles)
    p_p, p_r, p_f1 = _f1(predicate_correct, predicate_predicted, predicate_expected)
    r_p, r_r, r_f1 = _f1(role_correct, role_predicted, role_expected)
    mapping_status = Counter(item.get("status", "unknown") for item in mapped)
    composition_status = Counter(item.get("status", "unknown") for item in compositions.values())
    return {
        "version": "propbank-probe-v1",
        "cases": len(gold),
        "predicate": {
            "correct": predicate_correct,
            "predicted": predicate_predicted,
            "expected": predicate_expected,
            "precision": p_p,
            "recall": p_r,
            "f1": p_f1,
        },
        "argument_roles": {
            "correct": role_correct,
            "predicted": role_predicted,
            "expected": role_expected,
            "precision": r_p,
            "recall": r_r,
            "f1": r_f1,
        },
        "mapping_status": dict(mapping_status),
        "composition_status": dict(composition_status),
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    predicate = report["predicate"]
    roles = report["argument_roles"]
    lines = [
        "# PropBank probe report",
        "",
        f"Cases: {report['cases']}",
        "",
        "| Layer | Precision | Recall | F1 |",
        "|---|---:|---:|---:|",
        f"| predicate | {predicate['precision']} | {predicate['recall']} | {predicate['f1']} |",
        f"| argument roles | {roles['precision']} | {roles['recall']} | {roles['f1']} |",
        "",
        f"Mapping statuses: `{json.dumps(report['mapping_status'], sort_keys=True)}`",
        "",
        f"Composition statuses: `{json.dumps(report['composition_status'], sort_keys=True)}`",
        "",
    ]
    path.write_text("\n".join(lines))


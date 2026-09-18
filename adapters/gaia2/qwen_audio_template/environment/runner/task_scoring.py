"""Keep unscored executions out of official judging and failed-score annotations."""

from __future__ import annotations

import json


def install_evidence_guard(scenario, get_evidence):
    original = scenario.validate

    def validate(env):
        evidence = get_evidence()
        if evidence and evidence.get("score_basis") == "official_action_count_failure":
            from are.simulation.scenarios.validation_result import (
                ScenarioValidationResult,
            )

            return ScenarioValidationResult(
                success=False,
                rationale="Official necessary write-action counts differ; independent of unknown speech",
            )
        if evidence and evidence["task_score_status"] == "unresolved_speech":
            from are.simulation.scenarios.validation_result import (
                ScenarioValidationResult,
            )

            return ScenarioValidationResult(
                success=None, rationale="Not scored: unresolved interrupted speech"
            )
        return original(env)

    scenario.validate = validate


def mark_unscored_exports(output):
    for path in (output / "official").rglob("*.json"):
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("metadata"), dict):
            continue
        annotation = data["metadata"].setdefault("annotation", {})
        annotation["validation_decision"] = None
        annotation["comment"] = "Not scored: unresolved interrupted speech"
        path.write_text(json.dumps(data, ensure_ascii=False) + "\n")

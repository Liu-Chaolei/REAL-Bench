"""V8 retrospective real-ARE checks. Originals mounted read-only; no model calls."""

import json
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = ["/opt/runner", "/scripts"]
from are.simulation.data_handler.importer import JsonScenarioImporter
from are.simulation.environment import Environment
from are.simulation.scenarios.scenario import Scenario
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.types import EventLog
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from count_driver import CountDriver
from evidence import validate_grade
from gaia2_replay_export import main as replay_export
from gaia2_replay_export import normalize_export, parse_replay_parameter
from interruption_evidence import VERSION, verify_interruption_evidence
from task_contract import derive_contract


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected online model call")


class InterruptionTests(unittest.TestCase):
    def test_complete_0094_retains_full_official_judging(self):
        from gaia2_action_decision import loaded

        _scenario, _events, source_text, _contract = loaded(
            "/full-recording", "/full-source.json"
        )
        contract = derive_contract(_scenario, source_text, protocol=VERSION)
        root = Path("/comparisons/full-0094")
        shutil.copytree("/full-recording", root, dirs_exist_ok=True)
        (root / "integrity.json").unlink(missing_ok=True)
        result = json.loads((root / "result.json").read_text())
        env = Environment()
        env.event_log = EventLog.from_list_view(_events)
        driver = object.__new__(CountDriver)
        driver.output, driver.scenario, driver.source_text, driver.contract = (
            root,
            _scenario,
            source_text,
            contract,
        )
        driver.runtime = SimpleNamespace(env=env)
        driver.observation = SimpleNamespace(end_reason="idle_window")
        evidence = driver.final_evidence()
        result.update(
            protocol=VERSION,
            task_contract=contract,
            score_basis=evidence["score_basis"],
            interruption_evidence_sha256=evidence["interruption_evidence_sha256"],
            unverified_interruptions=[],
            decision_proof_sha256=None,
        )
        for name, value in [
            ("result.json", result),
            ("task-contract.json", contract),
            ("projection.json", evidence),
        ]:
            (root / name).write_text(json.dumps(value, indent=2) + "\n")
        output = Path("/comparisons/0094-full-replay.json")
        with patch.object(
            sys,
            "argv",
            [
                "replay",
                str(root),
                "--source-scenario",
                "/full-source.json",
                "--output",
                str(output),
            ],
        ):
            replay_export()
        report = json.loads(output.read_text())
        self.assertTrue(report["success"])
        self.assertEqual(report["judge_calls_replayed"], 9)
        self.assertEqual(report["fresh_model_calls"], 0)

    def test_two_archived_0084_runs_and_independent_replays(self):
        for label, missing in [
            ("v7", "CabApp__order_ride"),
            ("v6", "Contacts__delete_contact"),
        ]:
            with self.subTest(source=label):
                original = Path("/" + label + "-recording")
                source = Path("/" + label + "-source.json")
                root = Path("/comparisons") / label
                shutil.copytree(original, root, dirs_exist_ok=True)
                (root / "integrity.json").unlink(missing_ok=True)
                text = source.read_text()
                trace = json.loads(
                    next((root / "official/hf").glob("*.json")).read_text()
                )
                normalized, _ = normalize_export(trace, json.loads(text))
                parser = Scenario._parse_parameter_value
                with patch.object(
                    Scenario,
                    "_parse_parameter_value",
                    staticmethod(
                        lambda v, t, original=parser: parse_replay_parameter(
                            original, v, t
                        )
                    ),
                ):
                    scenario, events, _ = (
                        JsonScenarioImporter().import_from_json_to_benchmark(
                            json.dumps(normalized), load_completed_events=True
                        )
                    )
                preprocess_scenario(
                    scenario,
                    judge_config=GraphPerEventJudgeConfig(engine=forbidden),
                    offline_validation=True,
                )
                contract = derive_contract(scenario, text, protocol=VERSION)
                env = Environment()
                env.event_log = EventLog.from_list_view(events)
                driver = object.__new__(CountDriver)
                driver.output, driver.scenario, driver.source_text, driver.contract = (
                    root,
                    scenario,
                    text,
                    contract,
                )
                driver.runtime = SimpleNamespace(env=env)
                driver.observation = SimpleNamespace(end_reason="idle_window")
                evidence = driver.final_evidence()
                self.assertEqual(
                    evidence["score_basis"], "official_action_count_failure"
                )
                self.assertEqual(
                    evidence["speech_evidence_status"], "partial_interruption"
                )
                self.assertFalse(evidence["hard_errors"])
                proof = json.loads((root / "decision-proof.json").read_text())
                self.assertEqual(proof["missing"], {missing: 1})
                self.assertFalse(proof["extra"])
                association = verify_interruption_evidence(root)
                self.assertTrue(
                    any(
                        r["basis"] == "provider_response"
                        for r in association["responses"]
                    )
                )
                result = json.loads((root / "result.json").read_text())
                for key in [
                    "error",
                    "failure_cause",
                    "failure_details",
                    "failure_origin",
                ]:
                    result.pop(key, None)
                result.update(
                    protocol=VERSION,
                    status="completed",
                    success=False,
                    measurement_valid=True,
                    task_contract=contract,
                    task_score_status="scored",
                    speech_evidence_status="partial_interruption",
                    score_basis=evidence["score_basis"],
                    decision_proof_sha256=evidence["decision_proof_sha256"],
                    interruption_evidence_sha256=evidence[
                        "interruption_evidence_sha256"
                    ],
                    unverified_interruptions=[],
                    rationale="Retrospective V8 count failure; no new agent execution",
                )
                for name, value in [
                    ("result.json", result),
                    ("projection.json", evidence),
                    ("task-contract.json", contract),
                ]:
                    (root / name).write_text(json.dumps(value, indent=2) + "\n")
                self.assertEqual(validate_grade(result), {"reward": 0})
                output = Path("/comparisons") / (label + "-replay.json")
                with patch.object(
                    sys,
                    "argv",
                    [
                        "replay",
                        str(root),
                        "--source-scenario",
                        str(source),
                        "--output",
                        str(output),
                    ],
                ):
                    replay_export()
                report = json.loads(output.read_text())
                self.assertTrue(report["passed"])
                self.assertFalse(report["success"])
                self.assertEqual(report["fresh_model_calls"], 0)
                altered = dict(association)
                altered["responses"] = []
                (root / "interruption-evidence.json").write_text(json.dumps(altered))
                with self.assertRaisesRegex(ValueError, "differs"):
                    verify_interruption_evidence(root)
                (root / "interruption-evidence.json").write_text(
                    json.dumps(association, indent=2) + "\n"
                )


if __name__ == "__main__":
    unittest.main()

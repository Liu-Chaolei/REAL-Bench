"""V7 checks against fixed real ARE rules and archived 0084, without models."""

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path[:0] = ["/opt/runner", "/scripts"]
from action_decision import VERSION, action_count_proof, apply_decision
from are.simulation.data_handler.importer import JsonScenarioImporter
from are.simulation.environment import Environment
from are.simulation.scenarios.scenario import Scenario
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.types import EventLog
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from count_driver import CountDriver
from decision_audit import verify_decision
from evidence import evaluate_evidence, validate_grade
from gaia2_replay_export import normalize_export, parse_replay_parameter
from playback_timeline import build_timeline
from task_contract import derive_contract
from task_scoring import install_evidence_guard


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected model request")


def clone(event):
    result = copy.copy(event)
    result.action = copy.copy(event.action)
    result.action.args = dict(event.action.args)
    result.metadata = copy.copy(event.metadata)
    return result


def loaded(recording="/recording", source="/source.json"):
    text = Path(source).read_text()
    raw = json.loads(next((Path(recording) / "official/hf").glob("*.json")).read_text())
    data, _ = normalize_export(raw, json.loads(text))
    original = Scenario._parse_parameter_value
    with patch.object(
        Scenario,
        "_parse_parameter_value",
        staticmethod(lambda v, t: parse_replay_parameter(original, v, t)),
    ):
        scenario, events, _ = JsonScenarioImporter().import_from_json_to_benchmark(
            json.dumps(data), load_completed_events=True
        )
    preprocess_scenario(
        scenario,
        judge_config=GraphPerEventJudgeConfig(engine=forbidden),
        offline_validation=True,
    )
    return scenario, events, text, derive_contract(scenario, text, protocol=VERSION)


class ActionDecisionTests(unittest.TestCase):
    def test_0084_official_count_failure_and_live_guard(self):
        s, events, text, contract = loaded()
        proof = action_count_proof(s, events, contract, text)
        self.assertEqual(proof["missing"], {"Contacts__delete_contact": 1})
        self.assertEqual(proof["extra"], {})
        self.assertEqual(proof["model_calls"], 0)
        env = Environment()
        env.event_log = EventLog.from_list_view(events)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree("/recording", root, dirs_exist_ok=True)
            d = object.__new__(CountDriver)
            d.output, d.scenario, d.source_text, d.contract = root, s, text, contract
            d.runtime = SimpleNamespace(env=env)
            d.observation = SimpleNamespace(end_reason="idle_window")
            evidence = d.final_evidence()
            self.assertEqual(evidence["score_basis"], "official_action_count_failure")
            self.assertEqual(evidence["speech_evidence_status"], "partial_interruption")
            self.assertIsNone(evidence["task_submission_text"])
            install_evidence_guard(s, lambda: evidence)
            self.assertFalse(s.validate(env).success)

    def test_counts_matching_never_proves_success_or_wrong_args_failure(self):
        s, events, text, contract = loaded()
        missing = next(
            e
            for e in s.oracle_run_event_log
            if e.tool_name == "Contacts__delete_contact"
        )
        added = clone(missing)
        added.action.args["contact_id"] = "wrong-contact"
        proof = action_count_proof(s, events + [added], contract, text)
        self.assertEqual(proof["decision"], "inconclusive")
        evidence = evaluate_evidence(
            Path("/recording"), contract, end_reason="total_timeout"
        )
        self.assertEqual(
            apply_decision(evidence, proof)["task_score_status"], "unresolved_speech"
        )

    def test_failed_write_does_not_satisfy_requirement(self):
        s, events, text, contract = loaded()
        missing = clone(
            next(
                e
                for e in s.oracle_run_event_log
                if e.tool_name == "Contacts__delete_contact"
            )
        )
        missing.metadata.exception = RuntimeError("Deletion failed")
        proof = action_count_proof(s, events + [missing], contract, text)
        self.assertNotIn("Contacts__search_contacts", proof["actual_counts"])
        self.assertEqual(proof["missing"]["Contacts__delete_contact"], 1)

    def test_duplicate_write_is_official_failure_and_alias_is_canonical(self):
        s, events, text, contract = loaded()
        target = next(e for e in events if e.tool_name == "CabApp__order_ride")
        duplicate = clone(target)
        duplicate.event_id = "extra-cab"
        proof = action_count_proof(s, events + [duplicate], contract, text)
        self.assertEqual(proof["extra"], {"CabApp__order_ride": 1})
        self.assertNotIn("Cabs__order_ride", proof["actual_counts"])

    def test_complete_0094_still_uses_full_official_replay(self):
        from gaia2_replay_export import main as replay_export

        _s, _events, _text, contract = loaded("/full-recording", "/full-source.json")
        root = Path("/full-comparison")
        shutil.copytree("/full-recording", root, dirs_exist_ok=True)
        (root / "integrity.json").unlink(missing_ok=True)
        result = json.loads((root / "result.json").read_text())
        evidence = evaluate_evidence(
            root, contract, end_reason=result["timing"]["observation_end_reason"]
        )
        self.assertEqual(evidence["score_basis"], "official_full_judge")
        result.update(
            protocol=VERSION,
            task_contract=contract,
            score_basis="official_full_judge",
            decision_proof_sha256=None,
        )
        for name, data in [
            ("result.json", result),
            ("task-contract.json", contract),
            ("projection.json", evidence),
            ("playback-timeline.json", build_timeline(root)),
        ]:
            (root / name).write_text(json.dumps(data, indent=2) + "\n")
        with patch.object(
            sys,
            "argv",
            [
                "replay",
                str(root),
                "--source-scenario",
                "/full-source.json",
                "--output",
                "/full-comparison/replay-result.json",
            ],
        ):
            replay_export()
        report = json.loads((root / "replay-result.json").read_text())
        self.assertTrue(report["passed"])
        self.assertTrue(report["success"])
        self.assertEqual(report["fresh_model_calls"], 0)
        self.assertEqual(report["judge_calls_replayed"], 9)

    def test_timeline_places_cancelled_sixth_file_first(self):
        t = build_timeline(Path("/recording"))
        first = t["responses"][0]
        self.assertEqual(first["metadata"], "response-0006.json")
        self.assertAlmostEqual(first["played_audio_sec"], 21770 / 24000)
        self.assertLess(first["playback_cancel_sec"], 30)
        self.assertGreater(t["responses"][-1]["playback_end_sec"], 280)

    def test_archived_v7_comparison_and_tampering(self):
        s, events, text, contract = loaded()
        root = Path("/comparison")
        shutil.copytree("/recording", root, dirs_exist_ok=True)
        (root / "integrity.json").unlink(missing_ok=True)
        proof = action_count_proof(s, events, contract, text)
        evidence = apply_decision(
            evaluate_evidence(root, contract, end_reason="idle_window"), proof
        )
        result = json.loads((root / "result.json").read_text())
        result.update(
            protocol=VERSION,
            status="completed",
            success=False,
            measurement_valid=True,
            task_contract=contract,
            task_score_status="scored",
            score_basis=evidence["score_basis"],
            decision_proof_sha256=evidence["decision_proof_sha256"],
            rationale="Retrospective V7 official count failure; not a new live run",
        )
        for name, value in [
            ("task-contract.json", contract),
            ("projection.json", evidence),
            ("decision-proof.json", proof),
            ("playback-timeline.json", build_timeline(root)),
            ("result.json", result),
        ]:
            (root / name).write_text(json.dumps(value, indent=2) + "\n")
        validate_grade(result)
        self.assertEqual(
            verify_decision(root, result, s, events, text)["score_basis"],
            "official_action_count_failure",
        )
        changed = {**proof, "missing": {}}
        (root / "decision-proof.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "differs"):
            verify_decision(root, result, s, events, text)
        (root / "decision-proof.json").write_text(json.dumps(proof, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()

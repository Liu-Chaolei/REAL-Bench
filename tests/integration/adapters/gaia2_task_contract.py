"""Real ARE v5 contract/scoring checks. No live model calls; run in ARE image."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "/opt/runner")
from are.simulation.benchmark.scenario_loader import load_scenario
from are.simulation.environment import Environment
from are.simulation.scenario_runner import ScenarioRunner
from are.simulation.scenarios.config import ScenarioRunnerConfig
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.types import EventLog, EventType
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from task_contract import derive_contract, load_contract
from task_scoring import install_evidence_guard, mark_unscored_exports


def source(prefix):
    return next(Path("/dataset").glob(f"*-{prefix}-*/environment/scenario.json"))


def loaded(prefix):
    path = source(prefix)
    scenario, _ = load_scenario(
        path.read_text(), str(path), load_completed_events=False
    )
    # Mock only official semantic checks. Graph, hard arguments, counts and order remain real.
    preprocess_scenario(
        scenario,
        judge_config=GraphPerEventJudgeConfig(
            engine=lambda *a, **kw: ("[[True]] [[Match]]", None)
        ),
        offline_validation=True,
    )
    return scenario


class ContractTests(unittest.TestCase):
    def test_official_contract_is_stable_and_matches_packaged_contract(self):
        for prefix, kind in [
            ("0340", "action_only"),
            ("0036", "answer_only"),
            ("0094", "mixed"),
        ]:
            path = source(prefix)
            expected = json.loads(path.with_name("task-contract.json").read_text())
            generated = load_contract(path)
            self.assertEqual(generated, expected)
            self.assertEqual(generated["kind"], kind)
            self.assertEqual(
                derive_contract(loaded(prefix), path.read_text()), expected
            )

    def test_action_only_official_judge_checks_operations_without_spoken_text(self):
        value = loaded("0340")
        events = list(value.oracle_run_event_log)
        env = Environment()
        env.event_log = EventLog.from_list_view(events)
        self.assertTrue(value.validate(env).success)
        value = loaded("0340")
        bad = list(value.oracle_run_event_log)
        target = next(e for e in bad if e.event_type == EventType.AGENT)
        bad.remove(target)
        env.event_log = EventLog.from_list_view(bad)
        self.assertFalse(value.validate(env).success)

    def test_wrong_application_argument_still_fails(self):
        value = loaded("0340")
        events = list(value.oracle_run_event_log)
        index = next(
            i
            for i, e in enumerate(events)
            if e.event_type == EventType.AGENT
            and e.action.function_name == "save_apartment"
        )
        original = events[index]
        changed = copy.copy(original)
        changed.action = copy.copy(original.action)
        changed.action.args = {
            **original.action.args,
            "apartment_id": "definitely-not-the-required-property",
        }
        changed.action.resolved_args = changed.action.args.copy()
        events[index] = changed
        env = Environment()
        env.event_log = EventLog.from_list_view(events)
        self.assertFalse(value.validate(env).success)

    def test_unresolved_skips_real_judge_and_harbor_independent_annotation(self):
        value = loaded("0036")

        def forbidden(env):
            self.fail("Unresolved task invoked the official judge")

        value.validate = forbidden
        install_evidence_guard(
            value, lambda: {"task_score_status": "unresolved_speech"}
        )
        result = ScenarioRunner().run(
            ScenarioRunnerConfig(judge_only=True), value, completed_events=[]
        )
        self.assertIsNone(result.success)
        self.assertIsNone(result.exception)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "official/hf/test.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "metadata": {"annotation": {"validation_decision": "Invalid"}},
                        "completed_events": [],
                    }
                )
            )
            mark_unscored_exports(root)
            self.assertIsNone(
                json.loads(path.read_text())["metadata"]["annotation"][
                    "validation_decision"
                ]
            )

    def test_known_no_answer_is_a_failure_not_an_evidence_gap(self):
        value = loaded("0036")
        env = Environment()
        env.event_log = EventLog.from_list_view(
            [e for e in value.oracle_run_event_log if e.event_type != EventType.AGENT]
        )
        install_evidence_guard(value, lambda: {"task_score_status": "scored"})
        self.assertFalse(value.validate(env).success)


if __name__ == "__main__":
    unittest.main()

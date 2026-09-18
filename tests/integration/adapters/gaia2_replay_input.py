"""Regression for task submission text overwriting the official replay input."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/opt/runner")
import replay


class ImportReached(Exception):
    pass


class ReplayInputTests(unittest.TestCase):
    def test_importer_receives_complete_trace_for_each_task_contract(self):
        for version in ("gaia2-qwen-audio/5", "gaia2-qwen-audio/6"):
            for kind, answer in (
                ("action_only", None),
                ("answer_only", "Answer"),
                ("mixed", "Answer"),
            ):
                with (
                    self.subTest(version=version, kind=kind),
                    tempfile.TemporaryDirectory() as folder,
                ):
                    root = Path(folder)
                    (root / "official/hf").mkdir(parents=True)
                    events = (
                        []
                        if answer is None
                        else [
                            {
                                "event_type": "AGENT",
                                "action": {
                                    "app": "AgentUserInterface",
                                    "function": "send_message_to_user",
                                    "args": [{"name": "content", "value": answer}],
                                },
                            }
                        ]
                    )
                    trace = {
                        "completed_events": events,
                        "events": [],
                        "metadata": {"sentinel": "complete official export"},
                    }
                    (root / "official/hf/trace.json").write_text(json.dumps(trace))
                    result = {
                        "protocol": version,
                        "status": "completed",
                        "timing": {"observation_end_reason": "idle_window"},
                        "models": {
                            "GAIA2_JUDGE_MODEL": "unused",
                            "GAIA2_JUDGE_REASONING_EFFORT": "low",
                        },
                    }
                    (root / "result.json").write_text(json.dumps(result))
                    (root / "protocol.jsonl").write_text("")
                    (root / "task-contract.json").write_text(json.dumps({"kind": kind}))
                    projection = {"task_submission_text": answer}
                    (root / "projection.json").write_text(json.dumps(projection))

                    def import_trace(raw, expected=trace, **kwargs):
                        self.assertEqual(json.loads(raw), expected)
                        self.assertTrue(kwargs["load_completed_events"])
                        raise ImportReached

                    with (
                        patch.object(
                            sys,
                            "argv",
                            [
                                "replay",
                                str(root),
                                "--output",
                                str(root / "replayed.json"),
                            ],
                        ),
                        patch.object(replay, "validate_grade"),
                        patch.object(
                            replay, "evaluate_evidence", return_value=projection
                        ),
                        patch("native_recovery.verify_recorded_recovery"),
                        patch.object(replay, "ResponsesJudge"),
                        patch.object(
                            replay.JsonScenarioImporter,
                            "import_from_json_to_benchmark",
                            side_effect=import_trace,
                        ),
                        self.assertRaises(ImportReached),
                    ):
                        replay.main()


if __name__ == "__main__":
    unittest.main()

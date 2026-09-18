"""Offline, bounded CPU tests of v6 against real ARE. No provider calls."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, "/opt/runner")
from are.simulation.apps.agent_user_interface import AgentUserInterface
from are.simulation.apps.contacts import ContactsApp
from are.simulation.apps.system import SystemApp
from are.simulation.benchmark.scenario_loader import load_scenario
from are.simulation.environment import Environment, EnvironmentConfig
from are.simulation.scenarios.scenario import Scenario
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.types import EventLog, EventType
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from native_recovery import WATCHDOG_MESSAGE, verify_recorded_recovery
from passive_driver import PassiveDriver, PassiveProtocol
from run import app_tools
from runtime import Runtime


class PassiveIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        scenario = Scenario(
            apps=[ContactsApp(name="Contacts"), AgentUserInterface(), SystemApp()],
            start_time=1728975600,
        )
        scenario.initialize()
        self.scenario = scenario
        self.env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        self.env.run(scenario, wait_for_end=False)
        self.p = PassiveProtocol(self.root, lambda: (self.env.time_manager.time(), 1))
        self.rt = Runtime(self.env, self.p)
        self.rt.start()
        self.p.start()
        self.d = PassiveDriver(
            "http://unused",
            "token",
            self.root / "input.wav",
            self.root,
            list,
            self.p.clock,
            lambda text: self.fail("Action-only should not submit speech"),
            protocol=self.p,
            contract={
                "protocol": "gaia2-qwen-audio/6",
                "kind": "action_only",
                "requires_answer": False,
            },
        )
        self.d.runtime = self.rt
        self.rt.driver = self.d
        self.d.input_done = True
        self.d.user_context = ["Add the requested contact"]
        self.calls = []

        async def control(path, payload=None):
            self.calls.append((path, payload))
            if path == "state":
                return {"ready": True}
            if path == "barrier":
                return {"ok": True}
            self.fail("Unexpected state-changing recovery command")

        self.d.control = control
        self.d.proxy_state = AsyncMock(return_value={"active": 0, "sequence": 0})
        self.rt.proxy_barrier = AsyncMock()
        self.d.window.seconds = 0.02

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_fault_ready_agent_action_then_regular_observation_end(self):
        await self.d.observe_gateway_event(
            {"type": "error", "message": WATCHDOG_MESSAGE}
        )
        self.assertFalse(await self.d.advance_observation({"ready": True}))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.env.is_paused())
        self.assertTrue(self.d.transport_gate.is_set())
        self.assertIsNone(self.d.recovering_since)
        await self.d.observe_gateway_event(
            {"type": "voice.connection", "state": "connecting"}
        )
        await self.d.observe_gateway_event(
            {"type": "voice.ready", "inputSampleRate": 16000}
        )
        self.assertFalse(await self.d.advance_observation({"ready": True}))
        # Simulate an agent-originated application call, never initiated by recovery.
        tools = app_tools(self.scenario)
        name = next(n for n in tools if n.endswith("__add_new_contact"))
        await self.rt.call(
            name, tools[name], {"first_name": "Alice", "last_name": "Example"}
        )
        self.assertFalse(await self.d.advance_observation({"ready": True}))
        await asyncio.sleep(0.03)
        self.assertTrue(await self.d.advance_observation({"ready": True}))
        self.d.finalize_projection()
        summary = self.p.metrics()["native_recovery"]
        self.assertEqual(summary["completed_count"], 1)
        self.assertTrue(self.env.is_paused())
        self.assertTrue(all(path in {"state", "barrier"} for path, _ in self.calls))
        result = {"native_recovery": summary, "timing": self.p.metrics()}
        self.assertEqual(verify_recorded_recovery(self.root, result), summary)
        self.assertEqual(self.d.task_evidence["task_score_status"], "scored")

    async def test_original_budget_end_preserves_unfinished_recovery(self):
        await self.d.observe_gateway_event(
            {"type": "error", "message": WATCHDOG_MESSAGE}
        )
        self.p.end_reason = "total_timeout"
        self.p.end_sec = self.p.elapsed()
        self.d.finalize_projection()
        summary = self.p.metrics()["native_recovery"]
        self.assertEqual(summary["episodes"][0]["outcome"], "total_timeout")
        self.assertEqual(summary["completed_count"], 0)
        verify_recorded_recovery(
            self.root, {"native_recovery": summary, "timing": self.p.metrics()}
        )

    async def test_forged_ready_snapshot_is_not_a_valid_audit(self):
        await self.test_fault_ready_agent_action_then_regular_observation_end()
        file = self.root / "protocol.jsonl"
        rows = [json.loads(l) for l in file.read_text().splitlines()]
        for row in rows:
            if row["event"] == "native.recovery.ready_confirmed":
                row["ready_snapshot"] = False
        file.write_text("\n".join(json.dumps(r) for r in rows))
        with self.assertRaises(ValueError):
            verify_recorded_recovery(
                self.root,
                {
                    "native_recovery": self.p.metrics()["native_recovery"],
                    "timing": self.p.metrics(),
                },
            )


class OfficialScoreAfterRuntimeEvent(unittest.TestCase):
    def test_runtime_warning_does_not_force_zero_and_missing_work_still_fails(self):
        path = Path("/source.json")
        for complete in [True, False]:
            scenario, _ = load_scenario(
                path.read_text(), str(path), load_completed_events=False
            )
            preprocess_scenario(
                scenario,
                judge_config=GraphPerEventJudgeConfig(
                    engine=lambda *a, **kw: ("[[True]] [[Match]]", None)
                ),
                offline_validation=True,
            )
            events = list(scenario.oracle_run_event_log)
            if not complete:
                events = [e for e in events if e.event_type != EventType.AGENT]
            env = Environment()
            env.event_log = EventLog.from_list_view(events)
            # Recovery warnings are protocol evidence, not fabricated ARE actions.
            self.assertEqual(scenario.validate(env).success, complete)


if __name__ == "__main__":
    unittest.main()

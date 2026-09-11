"""Offline tests against real ARE, with synthetic transport and no model requests."""

import asyncio
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/opt/runner")
from are.simulation.apps.agent_user_interface import AgentUserInterface
from are.simulation.apps.contacts import ContactsApp
from are.simulation.apps.system import SystemApp
from are.simulation.environment import Environment, EnvironmentConfig
from are.simulation.scenarios.scenario import Scenario
from are.simulation.types import EventType
from audio_driver import AudioDriver
from run import app_tools
from runtime import Runtime
from window_driver import IncompleteAudibleTranscript, WindowDriver, WindowProtocol


class WindowRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scenario = Scenario(
            apps=[ContactsApp(name="Contacts"), AgentUserInterface(), SystemApp()],
            start_time=1728975600,
        )
        self.scenario.initialize()
        self.env = Environment(
            EnvironmentConfig(
                start_time=self.scenario.start_time, exit_when_no_events=False
            )
        )
        self.env.run(self.scenario, wait_for_end=False)
        self.p = WindowProtocol(self.root, lambda: (self.env.time_manager.time(), 1))
        self.runtime = Runtime(self.env, self.p)
        self.runtime.start()
        self.p.start()
        self.aui = self.env.get_app_with_class(AgentUserInterface)
        self.aui.set_wait_for_user_response(False)
        self.driver = WindowDriver(
            "http://mock",
            "token",
            self.root / "in.wav",
            self.root,
            list,
            self.p.clock,
            self.aui.send_message_to_user,
            protocol=self.p,
        )
        self.driver.runtime = self.runtime
        self.runtime.driver = self.driver
        self.driver.input_done = True
        self.driver.control = AsyncMock(return_value={"ready": True})
        self.driver.proxy_state = AsyncMock(return_value={"active": 0, "sequence": 0})
        self.runtime.proxy_barrier = AsyncMock()
        self.driver.window.seconds = 0.02

    async def asyncTearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_audio(self, i, text, complete=True):
        path = self.root / f"response-{i:04d}"
        with wave.open(str(path.with_suffix(".wav")), "wb") as f:
            f.setparams((1, 2, 24000, 0, "NONE", "none"))
            f.writeframes(b"\0\0" * 100)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "response_id": str(i),
                    "played_samples": 100,
                    "played_completely": complete,
                    "transcript": text,
                    "heard_transcript": text if complete else None,
                    "playback_end_sec": float(i),
                    "declared_kind": "progress",
                }
            )
        )

    async def test_receipt_then_real_action_then_single_official_projection(self):
        delivered = self.p.bind("response", "delivery", "final")
        delivered.played = True
        delivered.playback_end_sec = 0.01
        await self.p.commit(
            delivered,
            "Already done",
            self.aui.send_message_to_user,
            self.runtime.barrier,
        )
        self.assertFalse(
            any(
                e.action and e.action.function_name == "send_message_to_user"
                for e in self.env.event_log.list_view()
            )
        )
        tools = app_tools(self.scenario)
        name = next(n for n in tools if n.endswith("__add_new_contact"))
        await self.runtime.call(
            name, tools[name], {"first_name": "Alice", "last_name": "Example"}
        )
        self.write_audio(1, "Already done")
        self.write_audio(2, "Now actually done")

        async def transport_run(driver):
            self.assertFalse(await driver.advance_observation({"ready": True}))
            await asyncio.sleep(0.025)
            self.assertTrue(await driver.advance_observation({"ready": True}))

        with patch.object(AudioDriver, "run", transport_run):
            await self.driver.run()
        projection = json.loads((self.root / "projection.json").read_text())
        self.assertEqual(projection["text"], "Already done\n\nNow actually done")
        self.assertEqual(self.p.turn, 1)
        self.assertFalse(self.runtime.accepting)
        with self.assertRaises(RuntimeError):
            await self.runtime.call(
                name, tools[name], {"first_name": "Late", "last_name": "Action"}
            )
        self.assertTrue(self.env.is_paused())

    async def test_work_arriving_at_close_reopens_observation(self):
        self.assertFalse(await self.driver.advance_observation({"ready": True}))
        await asyncio.sleep(0.025)
        self.driver.control = AsyncMock(return_value={"ready": True, "pendingTasks": 1})
        self.assertFalse(await self.driver.advance_observation({"ready": True}))
        self.assertTrue(self.runtime.accepting)
        self.assertTrue(self.runtime.dispatch.is_set())
        self.assertFalse(self.env.is_paused())

    async def test_cancelled_audible_output_invalidates_projection(self):
        self.write_audio(1, "This was only partly spoken", complete=False)

        async def transport_run(driver):
            await driver.freeze("idle_window")

        with (
            patch.object(AudioDriver, "run", transport_run),
            self.assertRaises(IncompleteAudibleTranscript),
        ):
            await self.driver.run()
        self.assertFalse(
            json.loads((self.root / "projection.json").read_text())[
                "audible_transcript_complete"
            ]
        )
        self.assertEqual(self.p.turn, 0)

    async def test_timeout_does_not_become_normal_success(self):
        async def transport_run(driver):
            raise ExceptionGroup(
                "transport deadline", [TimeoutError("budget exceeded")]
            )

        with (
            patch.object(AudioDriver, "run", transport_run),
            self.assertRaises(ExceptionGroup),
        ):
            await self.driver.run()
        self.assertEqual(self.p.end_reason, "total_timeout")
        self.assertFalse(self.runtime.accepting)
        self.assertTrue(self.env.is_paused())


class OfficialSingleWindowScore(unittest.TestCase):
    def test_all_actions_and_one_answer_score_without_online_turn_triggers(self):

        from are.simulation.benchmark.scenario_loader import load_scenario
        from are.simulation.scenarios.scenario_imported_from_json.utils import (
            preprocess_scenario,
        )
        from are.simulation.types import EventLog
        from are.simulation.validation.configs import GraphPerEventJudgeConfig
        from observation import eligibility

        source = Path("/source.json")
        self.assertTrue(source.is_file(), "Packaged source fixture must be mounted")
        self.assertTrue(eligibility(json.loads(source.read_text()))["supported"])

        def scenario():
            value, _ = load_scenario(
                source.read_text(), str(source), load_completed_events=False
            )
            preprocess_scenario(
                value,
                judge_config=GraphPerEventJudgeConfig(
                    engine=lambda *args, **kwargs: ("[[True]] [[Match]]", None)
                ),
                offline_validation=True,
            )
            return value

        value = scenario()
        # Actual ARE graph/order/count checks run; semantic calls use a fixed fixture.
        events = list(value.oracle_run_event_log)
        aui = [
            e
            for e in events
            if e.event_type == EventType.AGENT
            and e.action.function_name == "send_message_to_user"
        ]
        self.assertLessEqual(len(aui), 1)
        env = Environment()
        env.event_log = EventLog.from_list_view(events)
        self.assertTrue(value.validate(env).success)
        value = scenario()
        env.event_log = EventLog.from_list_view(
            [e for e in events if e.event_type != EventType.AGENT]
        )
        self.assertFalse(value.validate(env).success)


if __name__ == "__main__":
    unittest.main()

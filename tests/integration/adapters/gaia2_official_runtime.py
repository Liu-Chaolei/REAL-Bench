"""Run in the pinned ARE image; all judge responses are mocked, no API calls.

docker run --rm --network none -v ...:/checks:ro ... python /checks/gaia2_official_runtime.py
"""

import asyncio
import copy
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("GAIA2_RUNNER_PATH", "/opt/runner"))
import uvicorn
from are.simulation.apps.agent_user_interface import AgentUserInterface
from are.simulation.apps.contacts import ContactsApp
from are.simulation.benchmark.scenario_loader import load_scenario
from are.simulation.environment import Environment, EnvironmentConfig
from are.simulation.scenarios.scenario import Scenario
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    preprocess_scenario,
)
from are.simulation.types import EventType
from are.simulation.validation.configs import GraphPerEventJudgeConfig
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from run import app_tools, mcp_app


class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_preserves_errors_and_hides_user_text(self):
        scenario = Scenario(
            apps=[ContactsApp(name="Contacts"), AgentUserInterface()],
            start_time=1728975600,
        )
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)
        tools = app_tools(scenario)
        self.assertFalse(any("AgentUserInterface" in name for name in tools))
        server = uvicorn.Server(
            uvicorn.Config(
                mcp_app(tools, lambda: False),
                host="127.0.0.1",
                port=0,
                log_level="error",
            )
        )
        async with asyncio.TaskGroup() as group:
            task = group.create_task(server.serve())
            try:
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
                port = server.servers[0].sockets[0].getsockname()[1]
                async with (
                    streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (
                        read,
                        write,
                        _,
                    ),
                    ClientSession(read, write) as client,
                ):
                    await client.initialize()
                    discovered = await client.list_tools()
                    self.assertEqual(set(tools), {t.name for t in discovered.tools})
                    name = next(n for n in tools if n.endswith("__add_new_contact"))
                    successful = await client.call_tool(
                        name, {"first_name": "Alice", "last_name": "Example"}
                    )
                    self.assertFalse(successful.isError)
                    failed = await client.call_tool(
                        name, {"first_name": "Bob", "last_name": "Example", "age": 999}
                    )
                    self.assertTrue(failed.isError)
                    events = [
                        e
                        for e in env.event_log.list_view()
                        if e.event_type == EventType.AGENT
                    ]
                    self.assertEqual(len(events), 2)
                    self.assertFalse(events[0].failed())
                    self.assertTrue(events[1].failed())
                    self.assertIsNotNone(events[1].metadata.exception)
                    self.assertGreaterEqual(events[1].event_time, scenario.start_time)
            finally:
                server.should_exit = True
                env.stop()


@unittest.skipUnless(
    os.environ.get("GAIA2_SCENARIO_PATH"), "Requires packaged scenario and filesystem"
)
class OfficialJudgeIntegration(unittest.TestCase):
    def make_scenario(self):
        path = Path(os.environ["GAIA2_SCENARIO_PATH"])
        scenario, _ = load_scenario(
            path.read_text(), str(path), load_completed_events=False
        )
        self.assertIsNotNone(scenario)
        # The mock only replaces semantic LLM checks; official graph/count/time checks execute.
        preprocess_scenario(
            scenario,
            judge_config=GraphPerEventJudgeConfig(
                engine=lambda *args, **kwargs: ("[[True]] [[Match]]", None),
            ),
            offline_validation=True,
            max_scenario_duration=1800,
        )
        self.assertIsNotNone(mcp_app(app_tools(scenario), lambda: False))
        return scenario

    def test_official_oracle_passes_and_missing_actions_fail(self):
        scenario = self.make_scenario()
        events = scenario.oracle_run_event_log
        env = Environment()
        from are.simulation.types import EventLog

        env.event_log = EventLog.from_list_view(events)
        self.assertTrue(scenario.validate(env).success)
        scenario = self.make_scenario()
        env.event_log = EventLog.from_list_view(
            [
                e
                for e in scenario.oracle_run_event_log
                if e.event_type != EventType.AGENT
            ]
        )
        self.assertFalse(scenario.validate(env).success)
        scenario = self.make_scenario()
        events = [copy.copy(event) for event in scenario.oracle_run_event_log]
        agent_event = next(
            event for event in events if event.event_type == EventType.AGENT
        )
        agent_event.metadata = copy.copy(agent_event.metadata)
        agent_event.metadata.exception = RuntimeError("operation failed")
        env.event_log = EventLog.from_list_view(events)
        self.assertFalse(scenario.validate(env).success)


class VoiceRuntimeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_real_judge_boolean_marker_matches_official_parser(self):
        import httpx
        from are.simulation.validation.utils.llm_utils import build_llm_checkers
        from responses_judge import ResponsesJudge

        def response(request):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "Evaluation: [[True]]"}
                            ],
                        }
                    ],
                },
            )

        judge = ResponsesJudge(
            "gpt-6-astra",
            "http://mock/v1",
            "test",
            client=httpx.Client(transport=httpx.MockTransport(response)),
        )
        try:
            checker = build_llm_checkers(judge)["sanity_checker"]
            self.assertIs(
                checker(
                    {
                        "agent_action_call": "content: /Documents/news",
                        "task": "Find the directory",
                    }
                ),
                True,
            )
        finally:
            judge.close()

    async def test_exclusive_assessment_delay_is_not_task_time(self):
        import tempfile
        import time

        from protocol import Protocol
        from runtime import Runtime

        scenario = Scenario(apps=[AgentUserInterface()], start_time=1728975600)
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)
        try:
            with tempfile.TemporaryDirectory() as directory:
                p = Protocol(Path(directory), lambda: (env.time_manager.time(), 1))
                runtime = Runtime(env, p)
                runtime.start()
                p.start()
                before = p.elapsed()
                wall = time.monotonic()
                async with runtime.assessment_scope():
                    await asyncio.sleep(0.15)
                    self.assertLess(p.elapsed() - before, 0.03)
                self.assertGreater(time.monotonic() - wall, 0.14)
                self.assertFalse(env.is_paused())
                self.assertTrue(runtime.dispatch.is_set())
        finally:
            env.stop()

    async def test_task_activity_during_grading_invalidates_measurement(self):
        import tempfile
        from types import SimpleNamespace

        from boundary import MeasurementError
        from protocol import Protocol
        from runtime import Runtime

        scenario = Scenario(apps=[AgentUserInterface()], start_time=1728975600)
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)

        async def control(path, payload=None):
            return {"ready": True, "userSpeaking": False, "activeResponses": 0}

        try:
            with tempfile.TemporaryDirectory() as directory:
                p = Protocol(Path(directory), lambda: (env.time_manager.time(), 1))
                runtime = Runtime(env, p)
                runtime.start()
                p.start()
                runtime.driver = SimpleNamespace(
                    control=control,
                    active_transport=0,
                    task_activity_serial=0,
                    set_transport_paused=lambda paused: None,
                )
                with self.assertRaises(MeasurementError):
                    async with runtime.assessment_scope():
                        runtime.driver.task_activity_serial += 1
                self.assertFalse(env.is_paused())
                self.assertTrue(runtime.dispatch.is_set())
        finally:
            env.stop()

    async def test_busy_assessment_keeps_real_time_and_releases_dispatch(self):
        import tempfile
        from types import SimpleNamespace

        from protocol import Protocol
        from runtime import Runtime

        scenario = Scenario(apps=[AgentUserInterface()], start_time=1728975600)
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)
        barriers = []

        async def control(path, payload=None):
            if path == "barrier":
                barriers.append(payload["paused"])
            return {"ready": True, "userSpeaking": False, "activeResponses": 1}

        try:
            with tempfile.TemporaryDirectory() as directory:
                p = Protocol(Path(directory), lambda: (env.time_manager.time(), 1))
                runtime = Runtime(env, p)
                runtime.start()
                p.start()
                runtime.driver = SimpleNamespace(
                    control=control,
                    active_transport=0,
                    task_activity_serial=0,
                    set_transport_paused=lambda paused: None,
                )
                before = p.elapsed()

                async def grade():
                    await asyncio.sleep(0.12)
                    self.assertTrue(runtime.dispatch.is_set())
                    self.assertFalse(env.is_paused())
                    self.assertFalse(barriers[-1])
                    runtime.driver.task_activity_serial += 1
                    return "progress"

                self.assertEqual(
                    await runtime.await_assessment(asyncio.create_task(grade())),
                    "progress",
                )
                self.assertGreater(p.elapsed() - before, 0.1)
                self.assertTrue(runtime.dispatch.is_set())
                self.assertFalse(env.is_paused())
        finally:
            env.stop()

    async def test_pending_asr_prevents_excluding_concurrent_grading_time(self):
        import tempfile
        from types import SimpleNamespace

        from protocol import Protocol
        from runtime import Runtime

        scenario = Scenario(apps=[AgentUserInterface()], start_time=1728975600)
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)

        async def control(path, payload=None):
            return {"ready": True, "userSpeaking": False, "activeResponses": 0}

        try:
            with tempfile.TemporaryDirectory() as directory:
                p = Protocol(Path(directory), lambda: (env.time_manager.time(), 1))
                runtime = Runtime(env, p)
                runtime.start()
                p.start()
                runtime.driver = SimpleNamespace(
                    control=control,
                    active_transport=0,
                    task_activity_serial=0,
                    pending_input_items={"unconfirmed-input"},
                    set_transport_paused=lambda paused: None,
                )
                before = p.elapsed()

                async def grade():
                    await asyncio.sleep(0.12)
                    self.assertFalse(env.is_paused())
                    self.assertTrue(runtime.dispatch.is_set())
                    return "progress"

                self.assertEqual(
                    await runtime.await_assessment(asyncio.create_task(grade())),
                    "progress",
                )
                self.assertGreater(p.elapsed() - before, 0.1)
        finally:
            env.stop()

    async def test_real_wait_does_not_fast_forward(self):
        import tempfile
        import time

        from are.simulation.apps.system import SystemApp
        from protocol import Protocol
        from runtime import Runtime

        scenario = Scenario(
            apps=[SystemApp(), AgentUserInterface()], start_time=1728975600
        )
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)
        try:
            with tempfile.TemporaryDirectory() as directory:
                protocol = Protocol(
                    Path(directory), lambda: (env.time_manager.time(), 1)
                )
                runtime = Runtime(env, protocol)
                runtime.start()
                protocol.start()
                system = env.get_app_with_class(SystemApp)
                before = time.monotonic()
                await asyncio.to_thread(system.wait_for_notification, timeout=1)
                self.assertGreaterEqual(time.monotonic() - before, 0.95)
                self.assertLess(protocol.elapsed(), 2)
                self.assertLess(abs(env.time_manager.offset), 0.2)
        finally:
            env.stop()

    async def test_checkpoint_barrier_waits_for_official_condition(self):
        import tempfile

        from are.simulation.scenarios.utils.turn_conditions import (
            turn_condition_wrapper,
        )
        from are.simulation.types import ConditionCheckEvent
        from protocol import Protocol
        from runtime import Runtime

        checked = []

        def trigger(env, turn):
            checked.append(turn)
            return True, {}

        condition = ConditionCheckEvent.from_condition(
            turn_condition_wrapper(trigger, 1, "control"),
            every_tick=1,
        ).with_id("condition_turn_1")
        scenario = Scenario(
            apps=[AgentUserInterface()], events=[condition], start_time=1728975600
        )
        scenario.initialize()
        env = Environment(
            EnvironmentConfig(start_time=scenario.start_time, exit_when_no_events=False)
        )
        env.run(scenario, wait_for_end=False)
        try:
            with tempfile.TemporaryDirectory() as directory:
                protocol = Protocol(
                    Path(directory), lambda: (env.time_manager.time(), 1)
                )
                runtime = Runtime(env, protocol)
                runtime.start()
                protocol.start()
                aui = env.get_app_with_class(AgentUserInterface)
                aui.set_wait_for_user_response(False)
                delivery = protocol.bind("r", "d", "checkpoint")
                delivery.played = True
                await asyncio.wait_for(
                    protocol.commit(
                        delivery,
                        "checkpoint",
                        aui.send_message_to_user,
                        runtime.barrier,
                    ),
                    5,
                )
                self.assertEqual(checked, [1])
                self.assertEqual(protocol.state, "running")
        finally:
            env.stop()


if __name__ == "__main__":
    unittest.main()

"""Official ARE runner with a speech-driven external agent and MCP apps."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
import traceback
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from action_decision import VERSION as COUNT_VERSION
from are.simulation.agents.agent_builder import AbstractAgentBuilder
from are.simulation.agents.agent_config_builder import AgentConfigBuilder
from are.simulation.agents.agent_execution_result import AgentExecutionResult
from are.simulation.agents.are_simulation_agent import RunnableARESimulationAgent
from are.simulation.agents.are_simulation_agent_config import (
    ARESimulationReactAgentConfig,
    LLMEngineConfig,
)
from are.simulation.apps.agent_user_interface import AgentUserInterface
from are.simulation.scenario_runner import ScenarioRunner
from are.simulation.scenarios.config import ScenarioRunnerConfig
from are.simulation.scenarios.scenario_imported_from_json.utils import (
    load_and_preprocess_scenario_str,
)
from audio_driver import AudioDriver, InfrastructureError, UserAbort
from boundary import BoundaryAssessor, MeasurementError
from count_driver import CountDriver, CountProtocol, InterruptionProtocol
from interruption_evidence import VERSION as INTERRUPTION_VERSION
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from native_recovery import POLICY as RECOVERY_POLICY
from native_recovery import VERSION as RECOVERY_VERSION
from observation import POLICY, eligibility
from observation import VERSION as WINDOW_VERSION
from passive_driver import PassiveDriver, PassiveProtocol
from protocol import VERSION, Protocol, ProtocolError
from pydantic import Field, create_model
from quality import interaction_report
from responses_judge import ResponsesJudge
from runtime import Runtime
from starlette.applications import Starlette
from starlette.routing import Mount
from task_contract import VERSION as TASK_VERSION
from task_contract import derive_contract
from task_driver import TaskDriver, TaskProtocol
from task_scoring import install_evidence_guard, mark_unscored_exports
from window_driver import WindowDriver, WindowProtocol


class QwenConfigBuilder(AgentConfigBuilder):
    def build(self, agent_name, **kwargs):
        if agent_name != "qwen-audio":
            return super().build(agent_name, **kwargs)
        return ARESimulationReactAgentConfig(agent_name=agent_name)


def app_tools(scenario):
    # No AUI getter may bypass ASR; only the playback driver writes user replies.
    return {
        (tool._public_name or tool.name): tool
        for tool in scenario.get_tools()
        if not isinstance(tool.class_instance, AgentUserInterface)
    }


def mcp_app(tools, stopped, runtime=None):
    server = Server("gaia2-apps")
    lock = asyncio.Lock()
    schemas = []
    for name, tool in tools.items():
        fields = {
            arg.name: (
                arg.type_obj or str,
                Field(
                    default=arg.default if arg.has_default else ...,
                    description=arg.description,
                ),
            )
            for arg in tool.args
        }
        model = create_model(name, **fields)
        schemas.append(
            Tool(
                name=name,
                description=tool._public_description
                or tool.function_description
                or name,
                inputSchema=model.model_json_schema(),
            )
        )

    if runtime:
        for name, description, properties, required in [
            (
                "evaluation_deliver",
                "Deliver a spoken checkpoint, final answer, clarification, failure, or progress through the frontend. Waits for complete playback. Official scoring follows the configured observation protocol. Use a unique delivery_id; do not return the same answer again from the coordinator.",
                {
                    "delivery_id": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "progress",
                            "checkpoint",
                            "final",
                            "clarification",
                            "failure",
                        ],
                    },
                    "content": {"type": "string"},
                },
                ["delivery_id", "kind", "content"],
            ),
            (
                "evaluation_state",
                "Declare whole-request state. Pure operation tasks may complete silently. waiting_environment is not completion. No synthetic reply will be supplied for waiting_user.",
                {
                    "state": {
                        "type": "string",
                        "enum": [
                            "running",
                            "waiting_environment",
                            "waiting_user",
                            "completed",
                            "failed",
                        ],
                    }
                },
                ["state"],
            ),
            (
                "evaluation_notifications",
                "Consume pending environment notifications for the current request. To wait use SystemApp wait_for_notification; it uses real elapsed time.",
                {},
                [],
            ),
        ]:
            schemas.append(
                Tool(
                    name=name,
                    description=description,
                    inputSchema={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                )
            )

    if runtime:
        schemas.append(
            Tool(
                name="evaluation_delivery_status",
                description="Inspect the trusted playback/commit status of an existing delivery. A model declaration cannot mark it played.",
                inputSchema={
                    "type": "object",
                    "properties": {"delivery_id": {"type": "string"}},
                    "required": ["delivery_id"],
                    "additionalProperties": False,
                },
            )
        )

    @server.list_tools()
    async def list_tools():
        return schemas

    @server.call_tool()
    async def call_tool(name, arguments):
        if stopped() or (runtime and not runtime.accepting):
            raise RuntimeError("Scenario is no longer running")
        if runtime and name.startswith("evaluation_"):
            await runtime.dispatch.wait()
            if not runtime.accepting:
                raise RuntimeError("Observation is sealed")
            arguments = arguments or {}
            if name == "evaluation_delivery_status":
                result = runtime.protocol.delivery_status(arguments["delivery_id"])
            elif name == "evaluation_deliver":
                result = await runtime.driver.deliver(**arguments)
            elif name == "evaluation_state":
                runtime.protocol.set_state(arguments["state"])
                result = {"state": runtime.protocol.state}
            elif name == "evaluation_notifications":
                result = runtime.protocol.take_notifications()
            else:
                raise ValueError(f"Unknown tool: {name}")
            return [TextContent(type="text", text=json.dumps(result))]
        if name not in tools:
            raise ValueError(f"Unknown tool: {name}")
        async with lock:
            # Invoke AppTool itself, including official augmentation/decorators.
            result = (
                await runtime.call(name, tools[name], arguments or {})
                if runtime
                else await asyncio.to_thread(tools[name], **(arguments or {}))
            )
            return [TextContent(type="text", text=str(result))]

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    # Mount at / handles both /mcp and /mcp/ without a POST redirect.
    return Starlette(routes=[Mount("/", app=handle)], lifespan=lifespan)


class QwenAgent(RunnableARESimulationAgent):
    def __init__(self, env, options):
        self.env = env
        self.options = options
        self.driver = None
        self.server = None
        self.stopped = False
        self.error = None

    def stop(self):
        self.stopped = True
        if self.driver:
            self.driver.stop_requested = True

    def run_scenario(self, scenario, notification_system=None, initial_agent_logs=None):
        if notification_system is None:
            raise ValueError("Official notification system is required")
        aui = self.env.get_app_with_class(AgentUserInterface)
        if aui is None:
            raise ValueError("Scenario has no user interface")
        aui.set_wait_for_user_response(False)
        windowed = getattr(self.options, "protocol", VERSION) in {
            WINDOW_VERSION,
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }
        task_aware = getattr(self.options, "protocol", VERSION) in {
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }
        passive = getattr(self.options, "protocol", VERSION) in {
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }
        counted = getattr(self.options, "protocol", VERSION) in {
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }
        protocol_class = (
            InterruptionProtocol
            if self.options.protocol == INTERRUPTION_VERSION
            else CountProtocol
            if counted
            else PassiveProtocol
            if passive
            else TaskProtocol
            if task_aware
            else WindowProtocol
            if windowed
            else Protocol
        )
        protocol = protocol_class(
            Path(self.options.output),
            lambda: (self.env.time_manager.time(), 0 if self.env.is_paused() else 1),
        )
        runtime = Runtime(self.env, protocol)
        self.protocol = protocol
        self.runtime = runtime
        self.env.time_increment_in_seconds = 1

        def notifications():
            now = datetime.fromtimestamp(self.env.time_manager.time(), tz=UTC)
            return [
                {
                    "id": hashlib.sha256(
                        f"{msg.timestamp}|{msg.message_type}|{msg.message}".encode()
                    ).hexdigest(),
                    "type": msg.message_type.value,
                    "message": msg.message,
                }
                for msg in notification_system.message_queue.get_by_timestamp(now)
            ]

        driver_class = (
            CountDriver
            if counted
            else PassiveDriver
            if passive
            else TaskDriver
            if task_aware
            else WindowDriver
            if windowed
            else AudioDriver
        )
        self.driver = driver_class(
            self.options.gateway,
            self.options.token,
            Path(self.options.audio),
            Path(self.options.output),
            notifications,
            lambda: (self.env.time_manager.time(), 0 if self.env.is_paused() else 1),
            aui.send_message_to_user,
            assessor=None
            if windowed
            else BoundaryAssessor(self.options.boundary_engine),
            assessment_wait=runtime.await_assessment,
            stop_file="/evaluation/stop-requested",
            timeout=min(
                self.options.timeout,
                self.env.duration or self.options.timeout,
                2400 if windowed else self.options.timeout,
            ),
            protocol=protocol,
            barrier=runtime.barrier,
            start_clock=runtime.start,
            track=self.options.track,
            text_input=self.options.text_input,
            **({"contract": self.options.task_contract} if task_aware else {}),
            **(
                {
                    "scenario": scenario,
                    "source_text": Path(self.options.scenario).read_text(),
                }
                if counted
                else {}
            ),
        )

        runtime.driver = self.driver
        if isinstance(self.driver, WindowDriver):
            self.driver.runtime = runtime

        async def execute():
            app = mcp_app(
                app_tools(scenario),
                lambda: self.stopped or self.env.stop_event.is_set(),
                runtime,
            )
            self.server = uvicorn.Server(
                uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
            )
            async with asyncio.TaskGroup() as group:
                server_task = group.create_task(self.server.serve())
                try:
                    while not self.server.started:
                        if server_task.done():
                            await server_task
                            raise InfrastructureError("MCP server failed to start")
                        await asyncio.sleep(0.05)
                    await self.driver.run()
                finally:
                    if (
                        isinstance(protocol, WindowProtocol)
                        and protocol.end_reason is None
                    ):
                        protocol.end_reason = (
                            "user_stop"
                            if Path("/evaluation/stop-requested").exists()
                            else "execution_error"
                        )
                        protocol.end_sec = protocol.elapsed()
                        protocol.record("observation.ended", reason=protocol.end_reason)
                    if isinstance(self.driver, PassiveDriver):
                        self.driver.close_recovery_evidence(protocol.end_reason)
                    self.execution_timing = protocol.metrics()
                    self.env.pause()
                    self.stopped = True
                    self.server.should_exit = True

        try:
            asyncio.run(execute())
        except BaseException as exc:
            self.error = exc
            raise
        self.finished_at = time.monotonic()
        return AgentExecutionResult(output="Audio conversation completed")


class QwenBuilder(AbstractAgentBuilder):
    def __init__(self, options):
        self.options = options
        self.agent = None

    def list_agents(self):
        return ["qwen-audio"]

    def build(self, agent_config, env=None, mock_responses=None):
        if env is None:
            raise ValueError("ARE environment is required")
        self.agent = QwenAgent(env, self.options)
        return self.agent


def leaf_errors(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaf_errors(child)]
    return [error]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="/evaluation/scenario.json")
    parser.add_argument("--audio", default="/evaluation/input.wav")
    parser.add_argument("--audio-metadata", default="/evaluation/audio.json")
    parser.add_argument("--gateway", default="http://main:3101")
    parser.add_argument("--output", default="/evaluation/results")
    parser.add_argument(
        "--track", choices=["audio", "system_text", "backend_text"], default="audio"
    )
    parser.add_argument("--timeout", type=float, default=2400)
    parser.add_argument(
        "--protocol",
        choices=[
            VERSION,
            WINDOW_VERSION,
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        ],
        default=VERSION,
    )
    options = parser.parse_args()
    options.text_input = None
    if options.track != "audio":
        data = json.loads(Path(options.scenario).read_text())
        users = [e for e in data["events"] if e.get("event_type") == "USER"]
        if len(users) != 1:
            raise ValueError("Text controls require exactly one user message")
        options.text_input = next(
            arg["value"]
            for arg in users[0]["action"]["args"]
            if arg["name"] == "content"
        )
    options.token = os.environ["QWAUDIO_EVALUATION_TOKEN"]
    output = Path(options.output)
    output.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "benchmark": "gaia2-qwen-audio",
        "protocol": options.protocol,
        "track": options.track,
        "status": "infrastructure_error",
        "success": None,
    }
    builder = QwenBuilder(options)
    responses_judge = None
    boundary_engine = None
    try:
        for key in (
            "GAIA2_JUDGE_MODEL",
            "GAIA2_JUDGE_PROVIDER",
            "GAIA2_JUDGE_ENDPOINT",
        ):
            if not os.environ.get(key):
                raise InfrastructureError(f"Missing {key}")
        if options.protocol in {
            WINDOW_VERSION,
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }:
            supported = eligibility(json.loads(Path(options.scenario).read_text()))
            if not supported["supported"] or options.track != "audio":
                raise InfrastructureError(
                    str(supported["reasons"]), "unsupported_protocol"
                )
            if os.environ.get("GAIA2_JUDGE_API") != "responses":
                raise InfrastructureError("V4 requires the recorded Responses judge")
            result["observation_policy"] = {
                **POLICY,
                "maximum_seconds": min(options.timeout, 2400),
            }
            result["boundary_responses_calls"] = 0
            if options.protocol in {
                RECOVERY_VERSION,
                COUNT_VERSION,
                INTERRUPTION_VERSION,
            }:
                result["observation_policy"].update(RECOVERY_POLICY)
            if options.protocol in {
                TASK_VERSION,
                RECOVERY_VERSION,
                COUNT_VERSION,
                INTERRUPTION_VERSION,
            }:
                result["observation_policy"]["partial_audio"] = (
                    "score_by_official_task_contract"
                )
        else:
            boundary_engine = ResponsesJudge(
                model=os.environ["GAIA2_JUDGE_MODEL"],
                endpoint=os.environ["GAIA2_JUDGE_ENDPOINT"],
                api_key=os.environ["OPENAI_API_KEY"],
                effort=os.environ.get("GAIA2_JUDGE_REASONING_EFFORT") or "low",
                api=os.environ.get("GAIA2_JUDGE_API", "chat"),
                record_path=output / "boundary-calls.jsonl",
            )
        options.boundary_engine = boundary_engine
        config = ScenarioRunnerConfig(
            agent="qwen-audio",
            model=os.environ.get("QWEN_AUDIO_AGENT_BACKEND_MODEL", "external"),
            model_provider="local",
            export=True,
            output_dir=str(output / "official"),
            trace_dump_format="both",
            max_turns=None,
            judge_engine_config=LLMEngineConfig(
                model_name=os.environ["GAIA2_JUDGE_MODEL"],
                provider=os.environ["GAIA2_JUDGE_PROVIDER"],
                endpoint=os.environ["GAIA2_JUDGE_ENDPOINT"],
            ),
        )
        if os.environ.get("GAIA2_JUDGE_API", "chat") == "responses":
            from are.simulation.benchmark.scenario_loader import load_scenario
            from are.simulation.scenarios.scenario_imported_from_json.utils import (
                get_scenario_duration,
                preprocess_scenario,
            )
            from are.simulation.validation.configs import GraphPerEventJudgeConfig

            responses_judge = ResponsesJudge(
                model=os.environ["GAIA2_JUDGE_MODEL"],
                endpoint=os.environ["GAIA2_JUDGE_ENDPOINT"],
                api_key=os.environ["OPENAI_API_KEY"],
                effort=os.environ.get("GAIA2_JUDGE_REASONING_EFFORT") or "low",
                record_path=output / "judge-calls.jsonl",
            )
            scenario, _ = load_scenario(
                Path(options.scenario).read_text(),
                options.scenario,
                load_completed_events=False,
            )
            if scenario is None:
                raise InfrastructureError("Could not load GAIA2 scenario")
            preprocess_scenario(
                scenario,
                judge_config=GraphPerEventJudgeConfig(engine=responses_judge),
                max_scenario_duration=get_scenario_duration(
                    scenario,
                    config.max_time_scenario_duration,
                    config.max_scenario_duration,
                ),
                tool_augmentation_config=config.tool_augmentation_config,
                offline_validation=options.protocol
                in {
                    WINDOW_VERSION,
                    TASK_VERSION,
                    RECOVERY_VERSION,
                    COUNT_VERSION,
                    INTERRUPTION_VERSION,
                },
                env_events_config=config.env_events_config,
            )
        else:
            scenario, _ = load_and_preprocess_scenario_str(
                config, Path(options.scenario).read_text()
            )
        if options.protocol in {
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }:
            contract = derive_contract(
                scenario, Path(options.scenario).read_text(), protocol=options.protocol
            )
            expected = json.loads(
                Path(options.scenario).with_name("task-contract.json").read_text()
            )
            if contract != expected:
                raise InfrastructureError(
                    "Frozen official contract differs", "task_contract_mismatch"
                )
            options.task_contract = contract
            result["task_contract"] = contract
            (output / "task-contract.json").write_text(
                json.dumps(contract, indent=2) + "\n"
            )
            install_evidence_guard(
                scenario,
                lambda: (
                    getattr(builder.agent.driver, "task_evidence", None)
                    if builder.agent
                    else None
                ),
            )
        judged = ScenarioRunner(
            agent_config_builder=QwenConfigBuilder(), agent_builder=builder
        ).run(config, scenario)
        result["post_execution_sec"] = (
            time.monotonic() - builder.agent.finished_at
            if builder.agent and hasattr(builder.agent, "finished_at")
            else None
        )
        execution_error = builder.agent.error if builder.agent else None
        if execution_error:
            errors = leaf_errors(execution_error)
            result["failure_details"] = [
                {"type": type(e).__name__, "message": str(e)} for e in errors
            ]
            if any(isinstance(e, UserAbort) for e in errors):
                result.update(
                    status="user_aborted",
                    success=None,
                    measurement_valid=False,
                    failure_cause="user_aborted",
                )
            elif any(isinstance(e, MeasurementError) for e in errors):
                result.update(
                    status="measurement_invalid",
                    success=None,
                    measurement_valid=False,
                    failure_cause=next(
                        (
                            getattr(e, "cause", None)
                            for e in errors
                            if getattr(e, "cause", None)
                        ),
                        "boundary_or_timing_invalid",
                    ),
                    error=str(execution_error),
                )
            elif all(isinstance(e, ProtocolError) for e in errors):
                result.update(
                    status="protocol_failure",
                    success=False,
                    error=str(execution_error),
                    failure_cause=next(
                        (
                            getattr(e, "cause", None)
                            for e in errors
                            if getattr(e, "cause", None)
                        ),
                        "agent_protocol_noncompletion",
                    ),
                )
            elif all(isinstance(e, TimeoutError) for e in errors):
                result.update(status="execution_timeout", success=False)
            else:
                raise execution_error
        elif (
            options.protocol
            in {TASK_VERSION, RECOVERY_VERSION, COUNT_VERSION, INTERRUPTION_VERSION}
            and builder.agent
            and getattr(builder.agent.driver, "task_evidence", None)
            and builder.agent.driver.task_evidence["task_score_status"]
            == "unresolved_speech"
        ):
            result.update(
                status="completed_unscored", success=None, measurement_valid=None
            )
            mark_unscored_exports(output)
        elif judged.exception:
            raise judged.exception
        elif judged.success is None:
            raise InfrastructureError("Official validator returned no decision")
        else:
            result.update(status="completed", success=judged.success)
        result.update(
            rationale=judged.rationale,
            duration_sec=judged.duration,
            export_path=judged.export_path,
        )
    except Exception as exc:  # noqa: BLE001 - persist failures for the separate verifier
        result.update(
            error=str(exc), traceback=traceback.format_exc(), measurement_valid=False
        )
        errors = leaf_errors(exc)
        result["failure_cause"] = next(
            (getattr(e, "cause", None) for e in errors if getattr(e, "cause", None)),
            "adapter_failure",
        )
        if result["failure_cause"] == "unsupported_protocol":
            result["status"] = "unsupported_protocol"
    finally:
        result.setdefault(
            "measurement_valid",
            result["status"] in {"completed", "execution_timeout", "protocol_failure"},
        )
        if boundary_engine:
            result["boundary_responses_calls"] = boundary_engine.calls
            boundary_engine.close()
        if responses_judge:
            result["judge_responses_calls"] = responses_judge.calls
            responses_judge.close()
        if builder.agent:
            if getattr(builder.agent, "protocol", None):
                result["timing"] = getattr(
                    builder.agent, "execution_timing", builder.agent.protocol.metrics()
                )
                result["timing"]["reconnect_count"] = builder.agent.driver.recoveries
                result["timing"]["reconnect_sec"] = (
                    builder.agent.driver.recovery_wait_sec
                )
                result["timing"]["input_audio_sec"] = (
                    builder.agent.driver.input_duration
                )
            builder.agent.stop()
            builder.agent.env.stop()
        # The proxy lives in a separate process. Stop admission and drain its
        # writers before hashing; backend retries can outlive the audio driver.
        try:
            sealed = httpx.post(
                "http://127.0.0.1:8001/__seal",
                headers={
                    "Authorization": f"Bearer {os.environ['QWAUDIO_EVALUATION_TOKEN']}"
                },
                timeout=650,
                trust_env=False,
            )
            sealed.raise_for_status()
            if sealed.json().get("sealed") is not True:
                raise RuntimeError("Backend evidence was not sealed")
        except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as exc:
            result.update(
                status="measurement_invalid",
                measurement_valid=False,
                success=None,
                finalization_error=type(exc).__name__,
            )
            result.setdefault("failure_cause", "artifact_finalization_failure")
        if options.protocol in {
            WINDOW_VERSION,
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }:
            if builder.agent and builder.agent.driver:
                result["observation_policy"]["maximum_seconds"] = (
                    builder.agent.driver.timeout
                )
            projection_path = output / "projection.json"
            if projection_path.exists():
                projection = json.loads(projection_path.read_text())
                result["audible_transcript_complete"] = projection[
                    "audible_transcript_complete"
                ]
            else:
                result["audible_transcript_complete"] = None
            result["audit_status"] = "pending"
        if options.protocol in {
            TASK_VERSION,
            RECOVERY_VERSION,
            COUNT_VERSION,
            INTERRUPTION_VERSION,
        }:
            evidence = (
                getattr(builder.agent.driver, "task_evidence", None)
                if builder.agent
                else None
            )
            result["task_score_status"] = (
                evidence["task_score_status"] if evidence else "invalid_execution"
            )
            result["speech_evidence_status"] = (
                evidence["speech_evidence_status"]
                if evidence
                else "not_collected"
                if options.protocol
                in {RECOVERY_VERSION, COUNT_VERSION, INTERRUPTION_VERSION}
                else "missing_or_corrupt"
            )
            if options.protocol in {COUNT_VERSION, INTERRUPTION_VERSION}:
                result["score_basis"] = (
                    evidence.get("score_basis") if evidence else None
                )
                result["decision_proof_sha256"] = (
                    evidence.get("decision_proof_sha256") if evidence else None
                )
            if options.protocol == INTERRUPTION_VERSION:
                result["interruption_evidence_sha256"] = (
                    evidence.get("interruption_evidence_sha256") if evidence else None
                )
                result["unverified_interruptions"] = (
                    evidence.get("unverified_interruptions", []) if evidence else []
                )
            if result.get("measurement_valid") is False:
                result["task_score_status"] = "invalid_execution"
        if options.protocol in {RECOVERY_VERSION, COUNT_VERSION, INTERRUPTION_VERSION}:
            result["native_recovery"] = result.get("timing", {}).get("native_recovery")
            cause = result.get("failure_cause", "")
            result["failure_origin"] = (
                "external_service"
                if cause.startswith("provider_")
                else "backend_service_or_profile"
                if cause == "backend_provider_failure"
                else "measurement_scope"
                if cause == "unsupported_recovery_state"
                else "unknown_gateway_error"
                if cause == "unclassified_gateway_error"
                else "measurement_or_execution"
                if cause
                else None
            )
            if result.get("speech_evidence_status") == "not_collected":
                result["evidence_collection_stage"] = "aborted_before_projection"
        result["audio"] = json.loads(Path(options.audio_metadata).read_text())
        result["models"] = {
            key: os.environ.get(key)
            for key in (
                "QWEN_AUDIO_REALTIME_MODEL",
                "QWEN_AUDIO_REALTIME_PROVIDER",
                "SPEECH_TO_SPEECH_REALTIME_URL",
                "QWEN_AUDIO_AGENT_BACKEND_MODEL",
                "GAIA2_JUDGE_MODEL",
                "GAIA2_JUDGE_API",
                "GAIA2_JUDGE_REASONING_EFFORT",
                "GAIA2_BACKEND_REASONING_EFFORT",
                "GAIA2_JUDGE_PROVIDER",
                "GAIA2_JUDGE_ENDPOINT",
            )
        }
        result["source"] = json.loads(os.environ.get("QWAUDIO_SOURCE_REVISION", "{}"))
        result["are_version"] = "1.2.0"
        result["images"] = json.loads(os.environ.get("QWAUDIO_IMAGE_IDS", "[]"))
        (output / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        )
        (output / "interaction-quality.json").write_text(
            json.dumps(interaction_report(output), indent=2, ensure_ascii=False) + "\n"
        )
        (output / "integrity.json").write_text(
            json.dumps(
                {
                    "protocol": options.protocol,
                    "files": {
                        str(path.relative_to(output)): hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
                        for path in sorted(output.rglob("*"))
                        if path.is_file() and path.name != "integrity.json"
                    },
                },
                indent=2,
            )
            + "\n"
        )
    if result["status"] in {
        "infrastructure_error",
        "measurement_invalid",
        "user_aborted",
        "unsupported_protocol",
    }:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Key-free contracts for the full-system adapter, not agent accuracy tests."""

import asyncio
import base64
from collections import deque

import pytest

from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.transcript import LiveTranscriptFragment
from tau2.environment.tool import Tool
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
from tau2.voice.audio_native.qwen_agent.discrete_time_adapter import (
    DiscreteTimeQwenAgentAdapter,
)
from tau2.voice.audio_native.qwen_agent.played_audio_transcription import (
    SegmentedPlayedAudioTranscriber,
)
from tau2.voice.audio_native.qwen_agent.provider import QwenAgentProvider


class Bridge:
    """A connected system whose next event batch is controlled by the test."""

    input_sample_rate = 16000
    session_id = "system-test"
    metadata = {"image_id": "sha256:test"}

    def __init__(self):
        self.is_connected = False
        self.events = deque()
        self.sent = []

    async def connect(self, policy, tools):
        self.is_connected = True
        self.policy = policy

    async def send(self, message):
        self.sent.append(message)

    async def next_event(self):
        while not self.events:
            await asyncio.sleep(0.001)
        return self.events.popleft()

    def drain(self):
        events, self.events = list(self.events), deque()
        return events

    async def disconnect(self):
        self.is_connected = False


class Observer:
    """Accept only the audio that the adapter says it actually played."""

    def __init__(self):
        self.played = []
        self.fragments = []

    def validate_config(self):
        pass

    async def connect(self):
        pass

    async def send(self, pcm):
        self.played.append(pcm)

    def drain(self):
        fragments, self.fragments = self.fragments, []
        return fragments

    async def close(self):
        pass


class FinalizingObserver(Observer):
    """Observer that exposes a final played-audio fragment during shutdown."""

    def __init__(self):
        super().__init__()
        self.closed = False
        self.flushed = False

    async def flush_pending(self):
        self.flushed = True
        return [
            LiveTranscriptFragment(speaker="agent", text="Tail", start_ms=0, end_ms=100)
        ]

    async def close(self):
        assert self.flushed
        self.closed = True


class FakeSegmentedTranscriber(SegmentedPlayedAudioTranscriber):
    """Key-free ASR implementation for exercising final pending audio."""

    def validate_config(self):
        pass

    async def _transcribe(self, pcm, *, purpose="primary"):
        return "tail"


def tool():
    def read_order(order_id: str) -> str:
        """Read an order.

        Args:
            order_id: Order identifier.
        """
        return order_id

    return Tool(read_order)


@pytest.fixture
def system():
    bridge, observer = Bridge(), Observer()
    adapter = DiscreteTimeQwenAgentAdapter(
        tick_duration_ms=20,
        config=QwenAgentConfig(),
        provider=bridge,
        transcriber=observer,
    )
    adapter.connect("Full policy", [tool()])
    try:
        yield adapter, bridge, observer
    finally:
        adapter.disconnect()


def audio(response="r", milliseconds=100):
    # Constant nonzero PCM makes truncation and playback byte counts measurable.
    return {
        "type": "audio",
        "response_id": response,
        "sample_rate": 24000,
        "audio": base64.b64encode(b"\x10\x10" * (24 * milliseconds)).decode(),
    }


def test_media_keeps_flowing_while_business_result_is_pending(system):
    adapter, bridge, observer = system
    bridge.events.extend(
        [
            {
                "type": "tool_call",
                "call_id": "c",
                "name": "read_order",
                "arguments": {"order_id": "1"},
            },
            audio(),
        ]
    )
    result = adapter.run_tick(b"\xff" * 160)
    assert result.tool_calls[0].id == "c"
    assert result.agent_audio_data
    assert result.media_timeline and result.speech_intervals
    assert (
        observer.played
        and len(observer.played[0]) == len(result.get_played_agent_audio()) * 2
    )
    adapter.send_tool_result("c", "official tool result")
    adapter.run_tick(b"\xff" * 160)
    returns = [e for e in bridge.sent if e["type"] == "tool_result"]
    assert len(returns) == 1 and returns[0]["content"] == "official tool result"
    assert sum(e["type"] == "input_audio" for e in bridge.sent) >= 2


def test_shutdown_flushes_final_observer_transcript():
    bridge, observer = Bridge(), FinalizingObserver()
    adapter = DiscreteTimeQwenAgentAdapter(
        tick_duration_ms=20,
        config=QwenAgentConfig(),
        provider=bridge,
        transcriber=observer,
    )
    adapter.connect("Full policy", [tool()])
    adapter.disconnect()
    assert observer.flushed and observer.closed
    assert adapter.final_live_transcript is not None
    assert adapter.final_live_transcript.fragments[0].text == "Tail"


def test_segmented_observer_flushes_a_short_pending_tail():
    async def scenario():
        transcriber = FakeSegmentedTranscriber(QwenAgentConfig())
        await transcriber.connect()
        await transcriber.send(b"\x10\x10" * (8000 * 18 // 10))
        assert transcriber._pending
        fragments = await transcriber.flush_pending()
        assert fragments and fragments[0].text == "tail "
        await transcriber.close()

    asyncio.run(scenario())


def test_interruption_discards_unplayed_audio_and_never_reads_gateway_text(system):
    adapter, bridge, observer = system
    bridge.events.extend(
        [
            audio(),
            {
                "type": "gateway_event",
                "event": {
                    "type": "transcript.final",
                    "content": "Future answer must stay hidden",
                },
            },
        ]
    )
    first = adapter.run_tick(b"\xff" * 160)
    assert first.proportional_transcript == ""
    observer.fragments = [LiveTranscriptFragment(speaker="agent", text="Heard prefix")]
    bridge.events.append({"type": "clear", "response_id": "r"})
    cut = adapter.run_tick(b"\xff" * 160)
    # Audio played before the clear arrived is retained; future audio is gone.
    assert not adapter.playback.pending_chunks()
    assert cut.proportional_transcript == "Heard prefix"
    assert cut.was_truncated and cut.truncated_audio_bytes > 0
    assert any(e.get("event") == "playback.cancelled" for e in bridge.sent)
    assert not any(e.get("event") == "playback.ended" for e in bridge.sent)


def test_playback_end_waits_for_buffer_to_drain(system):
    adapter, bridge, _ = system
    bridge.events.extend(
        [audio(milliseconds=40), {"type": "audio_done", "response_id": "r"}]
    )
    adapter.run_tick(b"\xff" * 160)
    assert not any(e.get("event") == "playback.ended" for e in bridge.sent)
    for _ in range(3):
        adapter.run_tick(b"\xff" * 160)
    assert sum(e.get("event") == "playback.started" for e in bridge.sent) == 1
    assert sum(e.get("event") == "playback.ended" for e in bridge.sent) == 1


def test_clear_without_pending_output_is_not_a_truncation(system):
    adapter, bridge, _ = system
    bridge.events.append({"type": "clear"})
    result = adapter.run_tick(b"\xff" * 160)
    assert not result.was_truncated
    assert result.truncated_audio_bytes == 0


@pytest.mark.parametrize("new_audio_first", [False, True])
def test_cancelling_old_response_keeps_new_response_backlog(system, new_audio_first):
    adapter, bridge, _ = system
    bridge.events.append(audio("old", milliseconds=100))
    adapter.run_tick(b"\xff" * 160)
    events = [{"type": "clear", "response_id": "old"}, audio("new", milliseconds=100)]
    bridge.events.extend(reversed(events) if new_audio_first else events)
    cut = adapter.run_tick(b"\xff" * 160)
    assert cut.was_truncated
    assert cut.truncated_audio_bytes > 0
    assert adapter.playback.pending_chunks()
    assert all(response == "new" for _, response in adapter.playback.pending_chunks())
    assert (
        adapter._played["old"] + cut.truncated_audio_bytes == adapter._received["old"]
    )
    bridge.events.append({"type": "audio_done", "response_id": "new"})
    for _ in range(8):
        adapter.run_tick(b"\xff" * 160)
    assert adapter._played["new"] == adapter._received["new"]
    assert any(
        e.get("event") == "playback.ended" and e.get("response_id") == "new"
        for e in bridge.sent
    )


def test_late_clear_for_completed_response_does_not_cut_new_reply(system):
    adapter, bridge, _ = system
    bridge.events.extend(
        [audio("old", milliseconds=20), {"type": "audio_done", "response_id": "old"}]
    )
    for _ in range(3):
        adapter.run_tick(b"\xff" * 160)
    assert "old" in adapter._terminal
    bridge.events.extend(
        [audio("new", milliseconds=100), {"type": "clear", "response_id": "old"}]
    )
    result = adapter.run_tick(b"\xff" * 160)
    assert not result.was_truncated
    assert adapter.playback.pending_chunks()


def test_playback_to_collector_preserves_completed_reply_after_idle_clear(system):
    from types import SimpleNamespace

    from tau2.data_model.diagnostics import DiagnosticEvidence
    from tau2.data_model.message import AssistantMessage, Tick, UserMessage
    from tau2.diagnostics.collect import delivered_inputs
    from tau2.diagnostics.conciseness.evidence import preflight
    from tau2.diagnostics.conciseness.models import FeedbackConfig
    from tau2.utils.transcript.views import utterance_interrupted

    adapter, bridge, observer = system
    ticks = []
    bridge.events.extend(
        [audio(milliseconds=100), {"type": "audio_done", "response_id": "r"}]
    )
    for index in range(12):
        if index == 8:
            # Ordinary caller turn after completion, not an interruption.
            bridge.events.append({"type": "clear", "response_id": "r"})
        if index == 9:
            observer.fragments = [
                LiveTranscriptFragment(
                    speaker="agent", text="Completed.", start_ms=0, end_ms=100
                )
            ]
        result = adapter.run_tick(b"\xff" * 160)
        ticks.append(
            Tick(
                tick_id=index,
                timestamp="2026-11-07T00:00:00",
                tick_duration_seconds=0.02,
                agent_chunk=AssistantMessage(
                    role="assistant",
                    content=result.proportional_transcript or None,
                    contains_speech=result.contains_speech,
                    utterance_ids=result.item_ids or None,
                    raw_data=result.model_dump(),
                ),
                user_chunk=UserMessage(
                    role="user",
                    content="Do the task" if index == 0 else None,
                    contains_speech=index == 0,
                    utterance_ids=["user"] if index == 0 else None,
                ),
            )
        )
    rows = delivered_inputs(SimpleNamespace(ticks=ticks, info={}), None)
    (reply,) = [r for r in rows if r.role == "assistant_context"]
    assert reply.text == "Completed."
    assert reply.complete and reply.unavailable_reason is None
    evidence = DiagnosticEvidence(
        simulation_id="s", task_id="t", domain="gaia2", user_inputs=rows
    )
    assert preflight(evidence, FeedbackConfig())["ready_windows"] == 1
    assert not any(utterance_interrupted([t.agent_chunk]) for t in ticks)


def test_duplicate_business_request_is_not_replayed(system):
    adapter, bridge, _ = system
    event = {
        "type": "tool_call",
        "call_id": "same",
        "name": "read_order",
        "arguments": {},
    }
    bridge.events.append(event)
    adapter.run_tick(b"\xff" * 160)
    bridge.events.append(event)
    with pytest.raises(RuntimeError, match="Duplicate"):
        adapter.run_tick(b"\xff" * 160)


def test_config_requires_live_semantics_and_contains_no_credentials():
    config = AudioNativeConfig(
        provider="qwen_agent", qwen_agent_config=QwenAgentConfig()
    )
    assert config.model == "qwen-audio-agent"
    assert config.realtime_generation_enabled
    blocking_config = AudioNativeConfig(
        provider="qwen_agent",
        qwen_agent_config=QwenAgentConfig(),
        realtime_generation=False,
        max_steps_seconds=1200,
    )
    assert not blocking_config.realtime_generation_enabled
    assert blocking_config.max_steps_ticks == 6000
    with pytest.raises(ValueError, match="environment variable names"):
        QwenAgentConfig(environment={"OPENAI_API_KEY": "literal-secret"})
    with pytest.raises(ValueError, match="controlled"):
        QwenAgentConfig(environment={"AGENT_PROTOCOL": "SOME_VARIABLE"})
    assert "qwen_agent_config" not in AudioNativeConfig().model_dump()


def test_provider_protocol_failure_always_releases_owned_runtime():
    import websockets

    async def scenario():
        class Runtime:
            token = "local-test"
            metadata = {}
            closed = False

            def start(self):
                return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"

            def close(self):
                self.closed = True

        async def handler(ws):
            await ws.recv()
            await ws.send('{"type":"ready","protocol":999,"input_sample_rate":16000}')

        runtime = Runtime()
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            provider = QwenAgentProvider(QwenAgentConfig(), runtime=runtime)
            with pytest.raises(ValueError, match="Incompatible"):
                await provider.connect("Public policy", [tool()])
            assert runtime.closed and not provider.is_connected

    asyncio.run(scenario())


def test_business_trajectory_replays_to_the_same_database():
    """The adapter's canonical calls/results work with the real τ evaluator."""
    from tau2.data_model.message import Tick
    from tau2.domains.mock.environment import get_environment, get_tasks
    from tau2.evaluator.evaluator_env import FullDuplexEnvironmentEvaluator
    from tau2.utils.utils import get_now

    environment = get_environment()
    task = next(t for t in get_tasks() if t.id == "create_task_1")
    bridge, observer = Bridge(), Observer()
    adapter = DiscreteTimeQwenAgentAdapter(
        tick_duration_ms=20,
        config=QwenAgentConfig(),
        provider=bridge,
        transcriber=observer,
    )
    adapter.connect(environment.get_policy(), environment.get_tools())
    ticks = []
    try:
        for index, action in enumerate(task.evaluation_criteria.actions):
            bridge.events.append(
                {
                    "type": "tool_call",
                    "call_id": f"business-{index}",
                    "name": action.name,
                    "arguments": action.arguments,
                }
            )
            result = adapter.run_tick(b"\xff" * 160)
            responses = [environment.get_response(call) for call in result.tool_calls]
            ticks.append(
                Tick(
                    tick_id=index,
                    timestamp=get_now(),
                    tick_duration_seconds=0.02,
                    agent_tool_calls=result.tool_calls,
                    agent_tool_results=responses,
                )
            )
            for response in responses:
                adapter.send_tool_result(
                    response.id, response.content, is_error=response.error
                )
        adapter.run_tick(b"\xff" * 160)
        reward = FullDuplexEnvironmentEvaluator.calculate_reward(
            environment_constructor=get_environment,
            task=task,
            full_trajectory=ticks,
        )
        assert reward.reward == 1.0
        assert sum(event["type"] == "tool_result" for event in bridge.sent) == len(
            ticks
        )
    finally:
        adapter.disconnect()


def test_complete_system_config_limits_and_defaults():
    from tau2.data_model.simulation import VoiceRunConfig

    audio = AudioNativeConfig(
        provider="qwen_agent", qwen_agent_config=QwenAgentConfig()
    )
    config = VoiceRunConfig(domain="retail", language="en", audio_native_config=audio)
    assert config.max_concurrency == 1
    assert config.max_retries == config.hallucination_retries == 0
    with pytest.raises(ValueError, match="domain declaring support"):
        VoiceRunConfig(domain="airline", audio_native_config=audio)
    with pytest.raises(ValueError, match="max_retries"):
        VoiceRunConfig(domain="retail", audio_native_config=audio, max_retries=1)


def test_shared_mounts_work_with_confinement_and_unsafe_overlaps_fail(monkeypatch):
    import json

    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig(environment={}))
    monkeypatch.setattr(QwenAgentConfig, "resolved_environment", lambda *_: {})
    commands = []
    mounts = [{"Destination": "/shared-home"}]
    security = {
        "Privileged": False,
        "MaskedPaths": [],
        "ReadonlyPaths": [],
        "SecurityOpt": [
            "seccomp=unconfined",
            "apparmor=unconfined",
            "systempaths=unconfined",
        ],
    }

    def command(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "image",
                        "Config": {
                            "Labels": {
                                "org.tau.qwen-agent.protocol": "1",
                                "org.tau.qwen-agent.confinement": "mount-namespace-v1",
                            }
                        },
                    }
                ]
            )
        if args[0] == "run":
            return "a" * 64
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Mounts": mounts,
                        "HostConfig": security,
                    }
                ]
            )
        if args[:2] == ("rm", "--force"):
            return ""
        if args[0] == "port":
            return "127.0.0.1:12345"
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_command", command)
    assert runtime.start() == "ws://127.0.0.1:12345/control"
    assert runtime.metadata["platform_mount_count"] == 1
    runtime.close()
    mounts[:] = [{"Destination": "/opt/reference"}]
    with pytest.raises(RuntimeError, match="overlaps"):
        runtime.start()
    assert commands[-1] == ("rm", "--force", "a" * 64)
    assert runtime.container_id is None
    mounts[:] = [{"Destination": "/shared-home"}]
    security["SecurityOpt"] = []
    with pytest.raises(RuntimeError, match="supported nonprivileged"):
        runtime.start()
    assert runtime.container_id is None


def test_delayed_observer_words_align_to_played_audio_not_later_reply():
    from tau2.data_model.message import AssistantMessage, Tick
    from tau2.data_model.transcript import LiveTranscriptTick
    from tau2.utils.transcript.views import agent_messages
    from tau2.utils.utils import get_now

    ticks = []
    for index in range(6):
        metadata = LiveTranscriptTick(
            session_id="recorded",
            origin="recorded_audio",
            alignment="played_audio",
            speech_intervals_ms=[(0, 500)] if index in (0, 4) else [],
            fragments=[
                LiveTranscriptFragment(
                    speaker="agent",
                    text="First answer",
                    start_ms=0,
                    end_ms=500,
                )
            ]
            if index == 4
            else [],
        )
        ticks.append(
            Tick(
                tick_id=index,
                timestamp=get_now(),
                tick_duration_seconds=1,
                agent_chunk=AssistantMessage(
                    role="assistant",
                    content=None,
                    raw_data={"live_transcript": metadata.model_dump()},
                ),
            )
        )
    messages = agent_messages(ticks)
    assert messages[0].content == "First answer"
    from tau2.utils.transcript.live import live_utterance_groups

    groups = live_utterance_groups(ticks)
    assert groups[1].chunks[0].content == ""
    assert (
        groups[1].chunks[0].raw_data["live_segment"]["association_status"]
        == "unmatched_audio"
    )
    assert len(messages) == 1  # missing ASR remains evidence, outside the text view


def test_start_timeout_cleans_up_a_container_created_before_the_cli_returned(
    monkeypatch,
):
    import json
    from pathlib import Path

    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig(environment={}))
    monkeypatch.setattr(QwenAgentConfig, "resolved_environment", lambda *_: {})
    removed = []

    def command(*args, **kwargs):
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "image",
                        "Config": {
                            "Labels": {
                                "org.tau.qwen-agent.protocol": "1",
                                "org.tau.qwen-agent.confinement": "mount-namespace-v1",
                            }
                        },
                    }
                ]
            )
        if args[0] == "run":
            Path(args[args.index("--cidfile") + 1]).write_text("a" * 64)
            raise TimeoutError("Docker client timed out")
        if args[:2] == ("rm", "--force"):
            removed.append(args[2])
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_command", command)
    with pytest.raises(TimeoutError):
        runtime.start()
    assert removed == ["a" * 64]
    assert runtime.container_id is None


def test_start_timeout_recovers_container_when_cidfile_was_not_written(monkeypatch):
    """A Docker CLI timeout must not strand a labeled bridge container."""
    import json

    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig(environment={}))
    monkeypatch.setattr(QwenAgentConfig, "resolved_environment", lambda *_: {})
    recovered = "b" * 64
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": "image",
                        "Config": {
                            "Labels": {
                                "org.tau.qwen-agent.protocol": "1",
                                "org.tau.qwen-agent.confinement": "mount-namespace-v1",
                            }
                        },
                    }
                ]
            )
        if args[0] == "run":
            raise TimeoutError("Docker client timed out before cidfile write")
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Mounts": [],
                        "HostConfig": {
                            "Privileged": False,
                            "MaskedPaths": [],
                            "ReadonlyPaths": [],
                            "SecurityOpt": [
                                "seccomp=unconfined",
                                "apparmor=unconfined",
                                "systempaths=unconfined",
                            ],
                        },
                    }
                ]
            )
        if args[0] == "port":
            return "127.0.0.1:12345"
        if args[:2] == ("rm", "--force"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_command", command)
    monkeypatch.setattr(runtime, "_find_labeled_containers", lambda: [recovered])
    assert runtime.start() == "ws://127.0.0.1:12345/control"
    run_args = next(args for args in calls if args[0] == "run")
    assert "--label" in run_args
    assert runtime.container_id == recovered
    runtime.close()
    assert runtime.container_id is None


def test_private_config_aliases_expansion_precedence_and_no_secret_serialization(
    tmp_path,
):
    path = tmp_path / "config.env"
    original = """DASHSCOPE_API_KEY="frontend-secret"
QWEN_AUDIO_REALTIME_BASE_URL=wss://example.invalid/realtime
QWEN_AUDIO_REALTIME_MODEL=qwen-audio-3.0-realtime-plus
GAIA2_BACKEND_API_KEY=backend-secret
GAIA2_BACKEND_API_BASE=https://example.invalid/v1
GAIA2_BACKEND_MODEL=MiniMax-M3
GAIA2_BACKEND_REASONING_EFFORT=low
QWEN_AUDIO_AGENT_BACKEND_MODEL=openai/${GAIA2_BACKEND_MODEL}
UNRELATED_JUDGE_KEY=judge-secret
UNRELATED=${MISSING_UNUSED}
"""
    path.write_text(original)
    config = QwenAgentConfig(config_env_file=str(path))
    env = config.resolved_environment({})
    assert env["OPENAI_MODEL"] == "MiniMax-M3"
    assert env["QWEN_AUDIO_REALTIME_API_KEY"] == "frontend-secret"
    assert env["QWEN_CODE_REASONING_EFFORT"] == "low"
    assert env["QWEN_AUDIO_REALTIME_URL"] == "wss://example.invalid/realtime"
    assert "UNRELATED_JUDGE_KEY" not in env
    assert "secret" not in config.model_dump_json()
    assert (
        config.resolved_environment({"QWEN_CODE_MODEL": "override"})["OPENAI_MODEL"]
        == "override"
    )
    assert path.read_text() == original
    custom = config.model_copy(
        update={"environment": {"OPENAI_MODEL": "QWEN_AUDIO_AGENT_BACKEND_MODEL"}}
    )
    assert custom.resolved_environment({})["OPENAI_MODEL"] == "MiniMax-M3"
    path.write_text(
        original.replace(
            "GAIA2_BACKEND_MODEL=MiniMax-M3",
            "GAIA2_BACKEND_MODEL=${LOOP}\nLOOP=${GAIA2_BACKEND_MODEL}",
        )
    )
    with pytest.raises(ValueError, match="Cyclic"):
        config.resolved_environment({})
    path.write_text(
        original.replace(
            "GAIA2_BACKEND_MODEL=MiniMax-M3", "GAIA2_BACKEND_MODEL=${MISSING}"
        )
    )
    with pytest.raises(ValueError, match="Missing environment variable MISSING"):
        config.resolved_environment({})
    path.write_text('bad syntax "synthetic-secret"')
    with pytest.raises(ValueError) as error:
        config.resolved_environment({})
    assert "synthetic-secret" not in str(error.value)


def test_container_logs_are_private_redacted_and_saved_before_removal(
    monkeypatch, tmp_path
):
    import subprocess

    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig())
    runtime.container_id = "owned-container"
    runtime.diagnostics_path = tmp_path / "trace.container.log"
    runtime.diagnostics_path.touch(mode=0o644)
    runtime._redactions.append("synthetic-backend-secret")
    removed = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout="failure: synthetic-backend-secret", stderr="\n"
        ),
    )

    def remove(*args, **kwargs):
        assert runtime.diagnostics_path.exists()
        removed.append(args)
        return ""

    monkeypatch.setattr(runtime, "_command", remove)
    runtime.close()
    assert runtime.diagnostics_path.read_text() == "failure: [REDACTED]\n"
    assert runtime.diagnostics_path.stat().st_mode & 0o777 == 0o600
    assert removed == [("rm", "--force", "owned-container")]
    assert runtime.container_id is None


def test_platform_wrapper_error_is_not_mistaken_for_success(monkeypatch):
    import subprocess

    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 0, stdout="", stderr="private-details\nError: exit status 1\n"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        runtime._command("run")
    assert "private-details" not in str(error.value)


def test_failed_filesystem_launcher_is_reported_before_socket_timeout(monkeypatch):
    from tau2.voice.audio_native.qwen_agent.runtime import DockerRuntime

    runtime = DockerRuntime(QwenAgentConfig())
    runtime.container_id = "a" * 64
    monkeypatch.setattr(
        runtime,
        "_command",
        lambda *args: '[{"State":{"Running":false,"ExitCode":125}}]',
    )
    with pytest.raises(RuntimeError, match="filesystem launcher exited"):
        runtime.check_running()


def test_initial_docker_tcp_readiness_race_does_not_fail_a_new_call(monkeypatch):
    import json

    import websockets

    async def scenario():
        class Runtime:
            token = "test"
            metadata = {}
            closed = False

            def start(self):
                return url

            def close(self):
                self.closed = True

        async def handler(ws):
            await ws.recv()
            await ws.send(
                json.dumps(
                    {
                        "type": "ready",
                        "protocol": 1,
                        "input_sample_rate": 16000,
                        "metadata": {},
                    }
                )
            )
            await ws.wait_closed()

        original = websockets.connect
        attempts = []

        async def connect(*args, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise websockets.exceptions.InvalidMessage("endpoint is not bound yet")
            return await original(*args, **kwargs)

        monkeypatch.setattr(websockets, "connect", connect)
        runtime = Runtime()
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            provider = QwenAgentProvider(QwenAgentConfig(), runtime=runtime)
            try:
                await provider.connect("Public policy", [tool()])
                assert provider.is_connected
                assert len(attempts) == 2
                assert all(value["proxy"] is None for value in attempts)
            finally:
                await provider.disconnect()
            assert runtime.closed

    asyncio.run(scenario())


def test_backend_channel_reports_overflow_and_never_enters_media_queue():
    from queue import Queue
    from types import SimpleNamespace

    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
    from tau2.voice.audio_native.qwen_agent.provider import QwenAgentProvider

    provider = QwenAgentProvider(QwenAgentConfig(), runtime=SimpleNamespace())
    provider.persistent_tasks_enabled = True
    provider.backend_events = Queue(maxsize=1)
    provider.record(
        {"type": "backend_task_state", "task_id": "native", "state": "running"}
    )
    provider.record(
        {"type": "backend_task_state", "task_id": "native", "state": "completed"}
    )
    assert provider.backend_evidence_gap is True
    assert provider.events.empty()
    drained = provider.drain_backend_events()
    assert len(drained) == 1 and drained[0]["received_monotonic"] > 0
    assert provider.drain_backend_events() == []


def test_backend_metric_input_excludes_transport_receipt_fields():
    from types import SimpleNamespace

    from tau2.data_model.diagnostics import BackendInput

    provider = QwenAgentProvider(QwenAgentConfig(), runtime=SimpleNamespace())
    provider.diagnostics_enabled = True
    provider.record(
        {
            "type": "diagnostic_backend",
            "id": "observed-1",
            "time": 1.0,
            "role": "submit",
            "content": "Cancel yoga classes.",
        }
    )
    row = BackendInput.model_validate(provider.backend_inputs["observed-1"])
    assert row.content == "Cancel yoga classes."
    assert "received_monotonic" not in provider.backend_inputs["observed-1"]

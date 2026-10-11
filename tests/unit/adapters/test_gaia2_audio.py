import importlib.util
import json
import sys
import wave
from pathlib import Path

import pytest

from adapters.gaia2.adapter import Gaia2Adapter
from adapters.gaia2.audio import find_asset, inspect_audio, read_manifest
from adapters.gaia2.qwen_audio_tasks import generate_audio_task
from harbor.models.task.config import TaskConfig

ROOT = Path(__file__).resolve().parents[3]
DRIVER = ROOT / "adapters/gaia2/qwen_audio_template/environment/runner/audio_driver.py"
sys.path.insert(0, str(DRIVER.parent))
spec = importlib.util.spec_from_file_location("gaia2_audio_driver_test", DRIVER)
driver_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver_module
spec.loader.exec_module(driver_module)


@pytest.fixture(autouse=True)
def offline_assessor(monkeypatch):
    from boundary import Decision

    class Assessor:
        def classify(self, transcript, **kwargs):
            return Decision(
                "final",
                [
                    {
                        "kind": "final",
                        "acts": ["answer"],
                        "reason": "Known transport fixture",
                    }
                ]
                * 2,
                "fixture",
            )

    original = driver_module.AudioDriver.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("assessor", Assessor())
        original(self, *args, **kwargs)

    monkeypatch.setattr(driver_module.AudioDriver, "__init__", init)


def wav(path, frames=320):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * frames)
    return path


def scenario_task(user_count=1):
    return Gaia2Adapter.metadata_from_record(
        config="search",
        split="validation",
        record={
            "id": "test-001",
            "scenario_id": "scenario-001",
            "data": json.dumps(
                {
                    "metadata": {"definition": {}},
                    "apps": [],
                    "events": [
                        {
                            "event_type": "USER",
                            "event_id": f"u{i}",
                            "action": {
                                "function": "send_message_to_agent",
                                "args": [
                                    {"name": "content", "value": "private task text"}
                                ],
                            },
                        }
                        for i in range(user_count)
                    ],
                }
            ),
        },
    )


def test_audio_integrity(tmp_path):
    path = wav(tmp_path / "input.wav")
    assert inspect_audio(path)["duration_sec"] == 0.02
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError, match="Truncated"):
        inspect_audio(path)


def test_empty_audio_rejected(tmp_path):
    with pytest.raises(ValueError, match="Empty"):
        inspect_audio(wav(tmp_path / "empty.wav", 0))


def test_manifest_paths_and_duplicates(tmp_path):
    manifest = tmp_path / "index.jsonl"
    row = {"config": "search", "source_id": "test-001", "audio_path": "audio.wav"}
    manifest.write_text(json.dumps(row) + "\n")
    assert read_manifest(manifest)[("search", "test-001")]["audio_path"] == str(
        tmp_path / "audio.wav"
    )
    manifest.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        read_manifest(manifest)


def test_mapping_and_multi_turn_exclusion(tmp_path):
    wav(tmp_path / "search/test-001.wav")
    asset = find_asset(scenario_task(), audio_dir=tmp_path, manifest={})
    assert asset.event_id == "u0"
    with pytest.raises(ValueError, match="unsupported_user_message_count"):
        find_asset(scenario_task(2), audio_dir=tmp_path, manifest={})
    with pytest.raises(ValueError, match="audio_event_id_mismatch"):
        find_asset(
            scenario_task(),
            audio_dir=None,
            manifest={
                ("search", "test-001"): {
                    "audio_path": str(tmp_path / "search/test-001.wav"),
                    "event_id": "wrong",
                }
            },
        )


def test_generated_task_keeps_evidence_out_of_main(tmp_path):
    audio = tmp_path / "audio"
    wav(audio / "search/test-001.wav")
    task = scenario_task()
    adapter = Gaia2Adapter(tmp_path / "tasks")
    asset = find_asset(task, audio_dir=audio, manifest={})
    output = generate_audio_task(adapter, task, asset, tmp_path / "qwen-source")
    cfg = TaskConfig.model_validate_toml((output / "task.toml").read_text())
    assert cfg.verifier.environment_mode.value == "separate"
    assert cfg.artifacts[0].service == "are-runner"
    assert "private task text" not in (output / "instruction.md").read_text()
    dockerfile = (output / "environment/Dockerfile").read_text()
    assert "scenario.json" not in dockerfile
    assert "COPY . /tests/" in (output / "tests/Dockerfile").read_text()
    assert generate_audio_task(adapter, task, asset, tmp_path / "qwen-source") is None


def test_idle_requires_all_work_to_finish():
    assert driver_module.idle_state({"ready": True})
    for key in ["userSpeaking", "activeResponses", "pendingPlayback", "pendingTasks"]:
        assert not driver_module.idle_state({"ready": True, key: 1})
    assert not driver_module.idle_state({"ready": False})


@pytest.mark.asyncio
async def test_only_played_replies_recorded_once(tmp_path):
    calls = []
    driver = driver_module.AudioDriver(
        "http://gateway",
        "token",
        tmp_path / "input.wav",
        tmp_path,
        list,
        lambda: (0, 1),
        calls.append,
    )
    response = driver_module.Playback(
        "unsafe/response-id", transcript="The result", pcm=bytearray(b"\0\0")
    )
    await driver.maybe_record_reply(response)
    assert calls == []
    response.ended = True
    response.generation_done = True
    response.delivery_kind = "final"
    await driver.maybe_record_reply(response)
    await driver.maybe_record_reply(response)
    assert calls == ["The result"]
    assert (tmp_path / "response-0001.wav").is_file()
    cancelled = driver_module.Playback(
        "cancelled", transcript="Wrong", ended=True, cancelled=True
    )
    await driver.maybe_record_reply(cancelled)
    assert calls == ["The result"]


@pytest.mark.asyncio
async def test_virtual_playback_receipts(tmp_path):
    events, replies = [], []
    driver = driver_module.AudioDriver(
        "http://gateway",
        "token",
        tmp_path / "input.wav",
        tmp_path,
        list,
        lambda: (0, 1),
        replies.append,
    )

    async def send(event):
        events.append(event["type"])

    driver.send = send
    response = driver_module.Playback(
        "r1",
        transcript="Done",
        pcm=bytearray(b"\0\0" * 24),
        generation_done=True,
        delivery_kind="final",
    )
    response.chunks.put_nowait(bytes(response.pcm))
    response.chunks.put_nowait(None)
    await driver.playback(response)
    assert events == ["playback.started", "playback.ended"]
    assert replies == ["Done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_driver_full_lifecycle_uses_audio_only(tmp_path, monkeypatch, interrupt):
    import asyncio
    import base64

    sent, replies = [], []

    class Gateway:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

        async def send(self, raw):
            event = json.loads(raw)
            sent.append(event)
            if event["type"] == "audio.append":
                if interrupt:
                    self.queue.put_nowait(
                        json.dumps(
                            {"type": "playback.clear", "reason": "user_interruption"}
                        )
                    )
                    self.queue.put_nowait(
                        json.dumps({"type": "error", "message": "Realtime 请求已取消"})
                    )
                for response in [
                    {
                        "type": "audio.delta",
                        "audio": base64.b64encode(bytes(48)).decode(),
                        "sampleRate": 24000,
                    },
                    {
                        "type": "transcript.final",
                        "role": "assistant",
                        "content": "The answer",
                    },
                    {
                        "type": "evaluation.delivery",
                        "deliveryId": "d1",
                        "kind": "final",
                    },
                    {"type": "audio.done"},
                ]:
                    self.queue.put_nowait(
                        json.dumps({"responseId": "reply-1", **response})
                    )

    monkeypatch.setattr(driver_module, "connect", lambda *args, **kwargs: Gateway())
    notifications = [{"type": "USER_MESSAGE", "message": "MUST NOT BE SENT AS TEXT"}]

    def pop_notifications():
        result = list(notifications)
        notifications.clear()
        return result

    driver = driver_module.AudioDriver(
        "http://gateway",
        "secret",
        tmp_path / "input.wav",
        tmp_path,
        pop_notifications,
        lambda: (1000, 1),
        replies.append,
        timeout=3,
        idle_seconds=0,
    )

    async def control(path, payload=None):
        return {"ready": True}

    async def stream():
        await driver.send(
            {"type": "audio.append", "audio": base64.b64encode(bytes(640)).decode()}
        )
        driver.input_done = True

    driver.control = control
    driver.stream_input = stream
    await driver.run()
    assert replies == ["The answer"]
    assert [e["type"] for e in sent] == [
        "connect",
        "audio.append",
        "playback.started",
        "playback.ended",
    ]
    assert all("MUST NOT" not in json.dumps(e) for e in sent)
    assert all(t.done() for t in driver.background)


def test_summary_keeps_infrastructure_failures_in_selected_denominator(tmp_path):
    from adapters.gaia2.summarize_audio import summarize

    for name, payload in [
        (
            "passed",
            {"status": "completed", "success": True, "audio": {"config": "search"}},
        ),
        (
            "error",
            {
                "status": "infrastructure_error",
                "success": None,
                "audio": {"config": "search"},
            },
        ),
        (
            "unscored",
            {
                "status": "completed_unscored",
                "success": None,
                "measurement_valid": None,
                "audio": {"config": "ambiguity"},
            },
        ),
    ]:
        directory = tmp_path / name / "agent"
        directory.mkdir(parents=True)
        (directory / "gaia2_qwen_audio_result.json").write_text(json.dumps(payload))
    summary = summarize(tmp_path, {"selected": 3})
    assert summary["executed"] == 3
    assert summary["not_executed"] == 0
    assert summary["pass_rate_selected"] == pytest.approx(1 / 3)
    assert summary["scored"] == 1
    assert summary["unscored"] == 1
    assert summary["statuses"]["infrastructure_error"] == 1


def test_agent_preflight_rejects_missing_settings_without_echoing_secrets(
    tmp_path, monkeypatch
):
    from adapters.gaia2.qwen_audio_agent import MODEL_ENV, Gaia2QwenAudioAgent

    for key in MODEL_ENV:
        monkeypatch.delenv(key, raising=False)
    agent = Gaia2QwenAudioAgent(
        logs_dir=tmp_path, extra_env={"DASHSCOPE_API_KEY": "private-value"}
    )
    with pytest.raises(ValueError) as error:
        agent.preflight()
    assert "GAIA2_JUDGE_MODEL" in str(error.value)
    assert "private-value" not in str(error.value)
    with pytest.raises(FileNotFoundError):
        Gaia2QwenAudioAgent(logs_dir=tmp_path, env_file=str(tmp_path / "missing.env"))


@pytest.mark.asyncio
async def test_harbor_wrapper_handles_empty_exec_streams_and_separates_credentials(
    tmp_path,
):
    from unittest.mock import AsyncMock

    from adapters.gaia2.qwen_audio_agent import Gaia2QwenAudioAgent
    from harbor.environments.base import BaseEnvironment, ExecResult
    from harbor.models.agent.context import AgentContext

    environment = AsyncMock(spec=BaseEnvironment)
    environment.exec.return_value = ExecResult(return_code=0)
    environment.service_exec.side_effect = [
        ExecResult(return_code=0),
        # The official runner reports ordinary task failures with a non-zero
        # process code; its downloaded status remains authoritative.
        ExecResult(return_code=1),
    ]

    async def download(source, target, *, service):
        assert service == "are-runner"
        target.write_text(json.dumps({"status": "completed", "success": True}))

    environment.service_download_file.side_effect = download
    agent = Gaia2QwenAudioAgent(
        logs_dir=tmp_path,
        extra_env={
            "DASHSCOPE_API_KEY": "frontend-and-backend-test-key",
            "QWEN_AUDIO_AGENT_BACKEND_MODEL": "openai/test-model",
            "GAIA2_BACKEND_API_KEY": "backend-test-key",
            "OPENCODE_CONFIG_CONTENT": '{"provider":{"openai":{"options":{"baseURL":"http://backend.invalid/v1"}}}}',
            "GAIA2_JUDGE_MODEL": "judge-model",
            "GAIA2_JUDGE_ENDPOINT": "http://judge.invalid/v1",
            "GAIA2_JUDGE_API_KEY": "judge-test-key",
        },
    )
    await agent.setup(environment)
    context = AgentContext()
    await agent.run("NEVER SEND THIS REFERENCE TEXT", environment, context)
    assert context.metadata["official_success"] is True
    assert (tmp_path / "are-runner.log").read_text() == "\n"
    commands = [call.args[0] for call in environment.exec.await_args_list]
    assert all("NEVER SEND" not in command for command in commands)
    runner_env = environment.service_exec.await_args.kwargs["env"]
    assert runner_env["OPENAI_API_KEY"] == "judge-test-key"
    assert "DASHSCOPE_API_KEY" not in runner_env
    start = next(
        call
        for call in environment.exec.await_args_list
        if "nohup node" in call.args[0]
    )
    assert "OPENAI_API_KEY" not in start.kwargs["env"]
    assert "GAIA2_JUDGE_API_KEY" not in start.kwargs["env"]


@pytest.mark.asyncio
async def test_harbor_wrapper_preserves_custom_opencode_provider(tmp_path):
    from unittest.mock import AsyncMock

    from adapters.gaia2.qwen_audio_agent import Gaia2QwenAudioAgent
    from harbor.environments.base import BaseEnvironment, ExecResult
    from harbor.models.agent.context import AgentContext

    environment = AsyncMock(spec=BaseEnvironment)
    environment.exec.return_value = ExecResult(return_code=0)
    environment.service_exec.return_value = ExecResult(return_code=0)

    async def download(source, target, **kwargs):
        target.write_text(json.dumps({"status": "completed", "success": True}))

    environment.service_download_file.side_effect = download
    config = {
        "provider": {
            "xi": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "https://backend.invalid/v1",
                    "apiKey": "backend-test-key",
                },
                "models": {"gpt-5.5": {"name": "GPT-5.5"}},
            }
        }
    }
    agent = Gaia2QwenAudioAgent(
        logs_dir=tmp_path,
        extra_env={
            "DASHSCOPE_API_KEY": "speech-test-key",
            "QWEN_AUDIO_AGENT_BACKEND_MODEL": "xi/gpt-5.5",
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "GAIA2_JUDGE_MODEL": "judge-model",
            "GAIA2_JUDGE_ENDPOINT": "http://judge.invalid/v1",
            "GAIA2_JUDGE_API_KEY": "judge-test-key",
        },
    )
    await agent.run("reference", environment, AgentContext())

    proxy_start = next(
        call
        for call in environment.service_exec.await_args_list
        if "model_proxy.py" in call.args[0]
    )
    assert proxy_start.kwargs["env"]["GAIA2_BACKEND_CONTROL_ONLY"] == "1"
    start = next(
        call for call in environment.exec.await_args_list if "nohup node" in call.args[0]
    )
    forwarded = json.loads(start.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    assert forwarded["provider"]["xi"]["options"]["baseURL"] == config["provider"]["xi"]["options"]["baseURL"]
    assert forwarded["provider"]["xi"]["options"]["apiKey"] == "backend-test-key"


def test_expected_user_interruption_does_not_mask_other_gateway_errors():
    cancellation = {"type": "error", "message": "Realtime 请求已取消"}
    assert driver_module.expected_interruption(cancellation, 100.0, 100.1)
    assert not driver_module.expected_interruption(cancellation, None, 100.1)
    assert not driver_module.expected_interruption(cancellation, 100.0, 103.0)
    assert not driver_module.expected_interruption(
        {"type": "error", "message": "Invalid API key"}, 100.0, 100.1
    )


@pytest.mark.asyncio
async def test_non_ali_profile_needs_no_dashscope_and_forwards_speech_config(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from adapters.gaia2.qwen_audio_agent import MODEL_ENV, Gaia2QwenAudioAgent
    from harbor.environments.base import BaseEnvironment, ExecResult
    from harbor.models.agent.context import AgentContext

    for key in MODEL_ENV:
        monkeypatch.delenv(key, raising=False)
    agent = Gaia2QwenAudioAgent(
        logs_dir=tmp_path,
        extra_env={
            "QWEN_AUDIO_REALTIME_PROVIDER": "speech-to-speech",
            "SPEECH_TO_SPEECH_REALTIME_URL": "ws://speech.invalid/v1/realtime",
            "QWEN_AUDIO_AGENT_BACKEND_MODEL": "openai/test-model",
            "GAIA2_BACKEND_API_KEY": "backend-test-key",
            "OPENCODE_CONFIG_CONTENT": '{"provider":{"openai":{"options":{"baseURL":"http://backend.invalid/v1"}}}}',
            "GAIA2_JUDGE_MODEL": "judge-model",
            "GAIA2_JUDGE_ENDPOINT": "http://judge.invalid/v1",
            "GAIA2_JUDGE_API_KEY": "EMPTY",
        },
    )
    agent.preflight()
    assert agent.runtime_env["QWEN_AUDIO_REALTIME_MODEL"] == ""
    environment = AsyncMock(spec=BaseEnvironment)
    environment.exec.return_value = ExecResult(return_code=0)
    environment.service_exec.return_value = ExecResult(return_code=0)

    async def download(source, target, **kwargs):
        target.write_text(json.dumps({"status": "completed", "success": True}))

    environment.service_download_file.side_effect = download
    await agent.run("reference", environment, AgentContext())
    start = next(
        c for c in environment.exec.await_args_list if "nohup node" in c.args[0]
    )
    assert start.kwargs["env"]["QWEN_AUDIO_REALTIME_PROVIDER"] == "speech-to-speech"
    assert (
        start.kwargs["env"]["SPEECH_TO_SPEECH_REALTIME_URL"]
        == "ws://speech.invalid/v1/realtime"
    )
    assert "DASHSCOPE_API_KEY" not in start.kwargs["env"]
    assert "QWEN_AUDIO_REALTIME_BASE_URL" not in start.kwargs["env"]


def load_responses_judge():
    path = DRIVER.parent / "responses_judge.py"
    spec = importlib.util.spec_from_file_location("gaia2_responses_judge_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ResponsesJudge


def test_responses_judge_sends_low_effort_and_keeps_official_checker_contract():
    import httpx

    Judge = load_responses_judge()

    def respond(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == "gpt-6-astra"
        assert payload["reasoning"] == {"effort": "low"}
        assert payload["stream"] is False
        assert payload["store"] is False
        assert "temperature" not in payload
        event = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "[[True]]"}],
                    },
                ],
                "usage": {"input_tokens": 3},
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(event) + "\n\n",
        )

    judge = Judge(
        "gpt-6-astra",
        "https://test.invalid/v1",
        "secret-test-key",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    try:
        text, usage = judge(
            [{"role": "user", "content": "Check the action"}],
            additional_trace_tags=["judge_llm"],
        )
        assert text == "[[True]]"
        assert usage["input_tokens"] == 3
        assert judge.calls == 1
    finally:
        judge.close()


def test_boundary_judge_uses_configured_dashscope_chat_transport():
    import httpx

    Judge = load_responses_judge()

    def respond(request):
        payload = json.loads(request.content)
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert payload["model"] == "qwen-plus"
        assert payload["enable_thinking"] is False
        assert payload["messages"][0]["role"] == "system"
        assert "input" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "[[True]]"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3},
            },
        )

    judge = Judge(
        "qwen-plus",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "secret-test-key",
        api="chat",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    try:
        text, usage = judge(
            [{"role": "system", "content": "Return [[True]] or [[False]]."}]
        )
        assert text == "[[True]]"
        assert usage["prompt_tokens"] == 3
    finally:
        judge.close()


def test_responses_judge_rejects_truncated_stream_and_redacts_error_credentials():
    import httpx

    Judge = load_responses_judge()

    def truncated(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"type":"response.output_text.delta","delta":"[[Match]]"}\n\n',
        )

    judge = Judge(
        "gpt-6-astra",
        "https://test.invalid/v1",
        "secret-test-key",
        client=httpx.Client(transport=httpx.MockTransport(truncated)),
    )
    try:
        with pytest.raises(RuntimeError, match="without a completion"):
            judge([])
    finally:
        judge.close()

    def unauthorized(request):
        return httpx.Response(401, text="Invalid secret-test-key")

    judge = Judge(
        "gpt-6-astra",
        "https://test.invalid/v1",
        "secret-test-key",
        client=httpx.Client(transport=httpx.MockTransport(unauthorized)),
    )
    try:
        with pytest.raises(RuntimeError) as error:
            judge([])
        assert "secret-test-key" not in str(error.value)
    finally:
        judge.close()


@pytest.mark.asyncio
async def test_backend_delivery_id_cannot_change_content_while_pending(tmp_path):
    driver = driver_module.AudioDriver(
        "http://gateway",
        "token",
        tmp_path / "in.wav",
        tmp_path,
        list,
        lambda: (0, 1),
        lambda text: None,
    )
    delivery = driver.protocol.declare("pending", "final")
    delivery.requested_content = "first answer"
    with pytest.raises(driver_module.ProtocolError, match="requested content"):
        await driver.deliver("pending", "final", "different answer")

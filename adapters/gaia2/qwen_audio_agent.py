"""Harbor lifecycle wrapper for full-system Qwen speech evaluation."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import override

from dotenv import dotenv_values

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.agent.context import AgentContext

MODEL_ENV = (
    "GAIA2_BACKEND_REASONING_EFFORT",
    "GAIA2_JUDGE_REASONING_EFFORT",
    "GAIA2_JUDGE_API",
    "QWEN_AUDIO_REALTIME_PROVIDER",
    "SPEECH_TO_SPEECH_REALTIME_URL",
    "SPEECH_TO_SPEECH_AUTH_TOKEN",
    "OPENCODE_CONFIG_CONTENT",
    "GAIA2_BACKEND_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_AUDIO_REALTIME_API_KEY",
    "QWEN_AUDIO_REALTIME_MODEL",
    "QWEN_AUDIO_REALTIME_BASE_URL",
    "QWEN_AUDIO_AGENT_BACKEND_MODEL",
    "GAIA2_JUDGE_MODEL",
    "GAIA2_JUDGE_PROVIDER",
    "GAIA2_JUDGE_ENDPOINT",
    "GAIA2_JUDGE_API_KEY",
    "OPENAI_API_KEY",
)


class Gaia2QwenAudioAgent(BaseAgent):
    def __init__(
        self,
        *args,
        env_file: str | None = None,
        scenario_timeout_sec: int = 2400,
        track: str = "audio",
        protocol_version: str = "gaia2-qwen-audio/3",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if env_file and not Path(env_file).is_file():
            raise FileNotFoundError(f"Evaluation settings file not found: {env_file}")
        values = dotenv_values(env_file) if env_file else {}
        self.runtime_env = {
            key: str(
                self._extra_env.get(key) or values.get(key) or os.environ.get(key) or ""
            )
            for key in MODEL_ENV
        }
        self.runtime_env["QWEN_AUDIO_REALTIME_PROVIDER"] = (
            self.runtime_env["QWEN_AUDIO_REALTIME_PROVIDER"] or "dashscope"
        )
        self.runtime_env["QWEN_AUDIO_AGENT_BACKEND_MODEL"] = (
            self.runtime_env["QWEN_AUDIO_AGENT_BACKEND_MODEL"] or self.model_name or ""
        )
        self.runtime_env["QWEN_AUDIO_REALTIME_MODEL"] = self.runtime_env[
            "QWEN_AUDIO_REALTIME_MODEL"
        ] or (
            "qwen-audio-3.0-realtime-plus"
            if self.runtime_env["QWEN_AUDIO_REALTIME_PROVIDER"] == "dashscope"
            else ""
        )
        self.runtime_env["GAIA2_JUDGE_PROVIDER"] = (
            self.runtime_env["GAIA2_JUDGE_PROVIDER"] or "openai"
        )
        self.runtime_env["GAIA2_JUDGE_API"] = (
            self.runtime_env["GAIA2_JUDGE_API"] or "chat"
        )
        if track not in {"audio", "system_text", "backend_text"}:
            raise ValueError("Unknown evaluation track")
        if protocol_version not in {"gaia2-qwen-audio/3", "gaia2-qwen-audio/4"}:
            raise ValueError("Unsupported evaluation protocol")
        if protocol_version.endswith("/4") and track != "audio":
            raise ValueError("V4 supports audio only")
        self.protocol_version = protocol_version
        self.track = track
        self.scenario_timeout = int(scenario_timeout_sec)
        self.token = secrets.token_urlsafe(32)

    @staticmethod
    @override
    def name() -> str:
        return "gaia2-qwen-audio"

    @override
    def version(self) -> str:
        return "0.3.0"

    def preflight(self) -> None:
        if self.runtime_env["GAIA2_JUDGE_API"] not in {"chat", "responses"}:
            raise ValueError("GAIA2_JUDGE_API must be chat or responses")
        missing = [
            key
            for key in (
                "QWEN_AUDIO_AGENT_BACKEND_MODEL",
                "GAIA2_JUDGE_MODEL",
                "GAIA2_JUDGE_ENDPOINT",
            )
            if not self.runtime_env[key]
        ]
        provider = self.runtime_env["QWEN_AUDIO_REALTIME_PROVIDER"]
        if provider == "dashscope":
            if not (
                self.runtime_env["DASHSCOPE_API_KEY"]
                or self.runtime_env["QWEN_AUDIO_REALTIME_API_KEY"]
            ):
                missing.append("DASHSCOPE_API_KEY or QWEN_AUDIO_REALTIME_API_KEY")
        elif provider == "speech-to-speech":
            if not self.runtime_env["SPEECH_TO_SPEECH_REALTIME_URL"]:
                missing.append("SPEECH_TO_SPEECH_REALTIME_URL")
        else:
            raise ValueError(f"Unsupported speech provider: {provider}")
        backend = self.runtime_env["QWEN_AUDIO_AGENT_BACKEND_MODEL"]
        if (
            backend
            and ("/" not in backend or backend.startswith("alibaba-cn/"))
            and not self.runtime_env["DASHSCOPE_API_KEY"]
        ):
            missing.append("DASHSCOPE_API_KEY (OpenCode alibaba-cn backend)")
        if self.runtime_env["OPENCODE_CONFIG_CONTENT"]:
            backend_config = json.loads(self.runtime_env["OPENCODE_CONFIG_CONTENT"])
            if not isinstance(backend_config, dict):
                raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        if not (
            self.runtime_env["GAIA2_JUDGE_API_KEY"]
            or self.runtime_env["OPENAI_API_KEY"]
        ):
            missing.append("GAIA2_JUDGE_API_KEY or OPENAI_API_KEY")
        if missing:
            raise ValueError(
                "Missing Qwen audio evaluation settings: " + ", ".join(missing)
            )
        if self.scenario_timeout <= 0:
            raise ValueError("scenario_timeout_sec must be positive")

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        self.preflight()
        if self.protocol_version == "gaia2-qwen-audio/4":
            preflight_code = (
                "import json,sys; from pathlib import Path; "
                "sys.path.insert(0, '/opt/runner'); from observation import eligibility; "
                "result=eligibility(json.loads(Path('/evaluation/scenario.json').read_text())); "
                "print(json.dumps(result)); sys.exit(0 if result['supported'] else 1)"
            )
            check = await environment.service_exec(
                "python -c " + shlex.quote(preflight_code), service="are-runner"
            )
            if check.return_code:
                raise ValueError(
                    "unsupported_protocol: "
                    + (check.stdout or "V4 eligibility preflight failed")
                )
        self.image_ids = []
        if isinstance(environment, DockerEnvironment):
            images = await environment._run_docker_compose_command(
                ["images", "--format", "json"]
            )
            raw = images.stdout or "[]"
            self.image_ids = json.loads(raw)
        result = await environment.exec(
            "mkdir -p /run/evaluation /state/backend/workspace /logs/agent && chmod 700 /run/evaluation && chown -R node:node /state/backend && chmod 755 /state /logs/agent",
            user="root",
        )
        if result.return_code:
            raise RuntimeError("Could not prepare Qwen runtime directories")
        with tempfile.TemporaryDirectory(prefix="qwen-evaluation-") as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "token": self.token,
                        "gatewayOrigin": "http://main:3101",
                        "mcpServers": [
                            {
                                "name": "are",
                                "type": "http",
                                "url": "http://are-runner:8000/mcp",
                                "headers": [],
                            }
                        ],
                    }
                )
            )
            await environment.upload_file(config, "/run/evaluation/config.json")
            await environment.exec("chmod 600 /run/evaluation/config.json", user="root")

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        # Only the trusted proxy owns GPT credentials. OpenCode receives a dummy
        # key, a private ARE endpoint and an explicit deny-by-default tool policy.
        backend_config = json.loads(self.runtime_env["OPENCODE_CONFIG_CONTENT"] or "{}")
        model = self.runtime_env["QWEN_AUDIO_AGENT_BACKEND_MODEL"].split("/", 1)[-1]
        provider = backend_config.get("provider", {}).get("openai", {})
        endpoint = provider.get("options", {}).get("baseURL")
        if not endpoint:
            raise ValueError(
                "Evaluation v2 requires an explicit OpenAI Responses backend baseURL"
            )
        proxy_env = {
            "GAIA2_BACKEND_ENDPOINT": endpoint,
            "GAIA2_BACKEND_API_KEY": self.runtime_env["GAIA2_BACKEND_API_KEY"]
            or provider.get("options", {}).get("apiKey", ""),
            "GAIA2_BACKEND_MODEL": model,
            "GAIA2_BACKEND_REASONING_EFFORT": self.runtime_env[
                "GAIA2_BACKEND_REASONING_EFFORT"
            ]
            or "low",
            "QWAUDIO_EVALUATION_TOKEN": self.token,
        }
        if not proxy_env["GAIA2_BACKEND_API_KEY"]:
            raise ValueError("Missing backend proxy credential")
        result = await environment.service_exec(
            "nohup python /opt/runner/model_proxy.py >/evaluation/proxy.log 2>&1 </dev/null &",
            service="are-runner",
            env=proxy_env,
        )
        if result.return_code:
            raise RuntimeError("Could not start model proxy")
        provider.setdefault("options", {}).update(
            baseURL="http://are-runner:8001/v1", apiKey="evaluation-proxy"
        )
        backend_config["provider"] = {"openai": provider}
        backend_config.setdefault("experimental", {})["mcp_timeout"] = (
            self.scenario_timeout + 600
        ) * 1000
        backend_config["permission"] = {"*": "deny", "are_*": "allow"}
        backend_config["tools"] = {"*": False, "are_*": True}
        main_env = {
            key: value
            for key, value in self.runtime_env.items()
            if value
            and key
            in {
                "DASHSCOPE_API_KEY",
                "QWEN_AUDIO_REALTIME_API_KEY",
                "QWEN_AUDIO_REALTIME_PROVIDER",
                "QWEN_AUDIO_REALTIME_MODEL",
                "QWEN_AUDIO_REALTIME_BASE_URL",
                "SPEECH_TO_SPEECH_REALTIME_URL",
                "SPEECH_TO_SPEECH_AUTH_TOKEN",
                "QWEN_AUDIO_AGENT_BACKEND_MODEL",
            }
        }
        main_env.update(
            {
                "OPENCODE_CONFIG_CONTENT": json.dumps(backend_config),
                "QWAUDIO_EVALUATION_CONFIG": "/run/evaluation/config.json",
                "QWAUDIO_EVALUATION_BACKEND_SANDBOX": "1",
                "OPENCODE_WORKSPACE": "/state/backend/workspace",
                "QWEN_AUDIO_AGENT_BACKEND_PERMISSION_MODE": "native",
                "AGENT_TIMEOUT_MS": str((self.scenario_timeout + 600) * 1000),
            }
        )
        revision = await environment.exec("cat /opt/qwen/revision.json")
        source_info = (
            json.loads(revision.stdout or "{}") if revision.return_code == 0 else {}
        )
        result = await environment.exec(
            "nohup node /opt/qwen/server/src/index.mjs >/logs/agent/qwen-gateway.log 2>&1 </dev/null &",
            env=main_env,
        )
        if result.return_code:
            raise RuntimeError("Could not start Qwen Gateway")
        # Health probes exercise backend startup, but contain no benchmark prompt.
        startup_deadline = time.monotonic() + 180
        while time.monotonic() < startup_deadline:
            health = await environment.exec(
                "curl -fsS --connect-timeout 2 --max-time 5 http://localhost:3101/api/health",
                timeout_sec=10,
            )
            if health.return_code == 0:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError(
                "Qwen Gateway did not become healthy; see qwen-gateway.log"
            )
        runner_env = {
            key: value
            for key, value in self.runtime_env.items()
            if value
            and (
                key.startswith("GAIA2_JUDGE")
                or key.endswith("MODEL")
                or key
                in {
                    "QWEN_AUDIO_REALTIME_PROVIDER",
                    "SPEECH_TO_SPEECH_REALTIME_URL",
                    "GAIA2_BACKEND_REASONING_EFFORT",
                }
            )
        }
        runner_env["QWAUDIO_EVALUATION_TOKEN"] = self.token
        runner_env["QWAUDIO_SOURCE_REVISION"] = json.dumps(source_info)
        runner_env["QWAUDIO_IMAGE_IDS"] = json.dumps(getattr(self, "image_ids", []))
        runner_env["OPENAI_API_KEY"] = (
            self.runtime_env["GAIA2_JUDGE_API_KEY"]
            or self.runtime_env["OPENAI_API_KEY"]
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        result = await environment.service_exec(
            f"python /opt/runner/run.py --track {self.track} --protocol {self.protocol_version} --timeout {shlex.quote(str(self.scenario_timeout))}",
            service="are-runner",
            env=runner_env,
            timeout_sec=self.scenario_timeout + 600,
        )
        (self.logs_dir / "are-runner.log").write_text(
            (result.stdout or "") + "\n" + (result.stderr or "")
        )
        await environment.download_file(
            "/logs/agent/qwen-gateway.log", self.logs_dir / "qwen-gateway.log"
        )
        local_result = self.logs_dir / "gaia2_qwen_audio_result.json"
        await environment.service_download_file(
            "/evaluation/results/result.json",
            local_result,
            service="are-runner",
        )
        payload = json.loads(local_result.read_text())
        context.metadata = {
            "benchmark": self.name(),
            "status": payload["status"],
            "official_success": payload.get("success"),
            "models": payload.get("models"),
        }
        if result.return_code or payload["status"] == "infrastructure_error":
            raise RuntimeError(
                "ARE/Qwen evaluation infrastructure error; see gaia2_qwen_audio_result.json"
            )

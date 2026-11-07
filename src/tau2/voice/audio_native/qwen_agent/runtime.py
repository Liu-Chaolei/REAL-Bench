"""Lifecycle of an isolated, unmodified Qwen Gateway and native backend."""

import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from tau2.config import DEFAULT_QWEN_AGENT_PORT
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig


class DockerRuntime:
    """Own one container with mandatory inherited filesystem confinement."""

    def __init__(self, config: QwenAgentConfig):
        self.config = config
        self.container_id: str | None = None
        self.token = secrets.token_urlsafe(32)
        self.metadata: dict = {}
        self.diagnostics_path: Path | None = None
        self._redactions: list[str] = [self.token]

    def _command(self, *args, env=None, missing_ok=False):
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=self.config.boot_timeout_seconds,
            env=env,
        )
        # The platform wrapper can print an error while itself exiting zero.
        wrapper_failure = re.search(
            r"(?m)^Error: exit status [1-9][0-9]*\s*$",
            result.stdout + "\n" + result.stderr,
        )
        if result.returncode or wrapper_failure:
            if missing_ok and "No such container" in result.stdout + result.stderr:
                return ""
            # Docker stderr can include credential-bearing command arguments.
            reason = (
                "platform wrapper error"
                if wrapper_failure
                else f"exit {result.returncode}"
            )
            raise RuntimeError(f"Docker {args[0]} failed ({reason})")
        return result.stdout.strip()

    def start(self) -> str:
        """Start the fixed image and return its loopback control URL."""
        image = json.loads(self._command("image", "inspect", self.config.image))[0]
        labels = image.get("Config", {}).get("Labels") or {}
        supported_domains = labels.get("org.tau.qwen-agent.domains", "retail").split(
            ","
        )
        if (getattr(self.config, "domain", None) or "retail") not in supported_domains:
            raise ValueError(
                "The configured Qwen image does not declare support for this domain; build the updated integration image"
            )
        if (
            labels.get("org.tau.qwen-agent.protocol") != "1"
            or labels.get("org.tau.qwen-agent.confinement") != "mount-namespace-v1"
        ):
            raise ValueError(
                "Build the Qwen Agent image with the supplied build script"
            )
        if (
            self.config.speech_to_speech
            and labels.get("org.tau.speech-to-speech") != "1.1.0"
        ):
            raise ValueError(
                "Build the Speech-to-Speech 1.1.0 image for this configuration"
            )
        if (
            self.config.speech_to_speech
            and self.config.speech_to_speech.llm_fallback_api_key_env
            and labels.get("org.tau.speech-llm-fallback") != "1"
        ):
            raise ValueError(
                "Build the frontend LLM fallback image for this configuration"
            )
        if (
            self.config.speech_to_speech
            and self.config.speech_to_speech.asr_model == "qwen-audio-3.1-asr-flash"
            and labels.get("org.tau.speech-asr.qwen") != "1"
        ):
            raise ValueError("Build the Qwen frontend ASR image for this configuration")
        environment = dict(os.environ)
        args = [
            "run",
            "--detach",
            "--init",
            # These let the trusted launcher build a private filesystem/PID
            # view. It drops all capabilities and locks that view before exec.
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "--security-opt",
            "systempaths=unconfined",
            "--publish",
            f"127.0.0.1::{DEFAULT_QWEN_AGENT_PORT}",
            "--env",
            "TAU_BRIDGE_TOKEN",
        ]
        environment["TAU_BRIDGE_TOKEN"] = self.token
        selected = self.config.resolved_environment(dict(os.environ))
        if self.config.speech_to_speech:
            selected["TAU_S2S_BOOT_TIMEOUT_MS"] = str(
                round(self.config.boot_timeout_seconds * 1000)
            )
        for target, value in selected.items():
            environment[target] = value
            args.extend(["--env", target])
            if any(part in target for part in ("KEY", "TOKEN", "SECRET")):
                self._redactions.append(value)
        args.append(self.config.image)
        with tempfile.TemporaryDirectory(prefix="tau-qwen-container-") as directory:
            cidfile = Path(directory) / "container-id"
            args[1:1] = ["--cidfile", str(cidfile)]
            try:
                container_id = self._command(*args, env=environment)
                if not re.fullmatch(r"[a-f0-9]{64}", container_id):
                    raise RuntimeError("Docker did not return a container ID")
                self.container_id = container_id
            except BaseException:
                # A timed-out CLI can leave a successfully created container.
                # Its client-side cidfile identifies only this launch attempt.
                if cidfile.exists():
                    value = cidfile.read_text().strip()
                    if re.fullmatch(r"[a-f0-9]{64}", value):
                        self.container_id = value
                        self.close()
                raise
        try:
            actual = json.loads(self._command("inspect", self.container_id))[0]
            host_config = actual.get("HostConfig", {})
            if (
                host_config.get("Privileged")
                or not {"seccomp=unconfined", "apparmor=unconfined"}.issubset(
                    host_config.get("SecurityOpt") or []
                )
                # Docker consumes systempaths=unconfined into these fields.
                or host_config.get("MaskedPaths") != []
                or host_config.get("ReadonlyPaths") != []
            ):
                raise RuntimeError(
                    "Private filesystem view requires the supported nonprivileged launch options"
                )
            # Shared HPC mounts are omitted from the private filesystem view.
            # Do not let injected storage hide beneath an allowed image root.
            system_roots = (
                "/opt",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc",
                "/proc",
                "/sys",
                "/dev",
            )
            system_files = {
                "/etc/passwd",
                "/etc/group",
                "/etc/shadow",
                "/etc/sudoers",
                "/etc/hosts",
                "/etc/hostname",
                "/etc/resolv.conf",
                "/etc/localtime",
                "/etc/timezone",
                "/usr/bin/vc",
            }
            for mount in actual.get("Mounts", []):
                destination = mount["Destination"].rstrip("/") or "/"
                if destination in system_files:
                    continue
                if destination == "/" or any(
                    destination == root or destination.startswith(root + "/")
                    for root in system_roots
                ):
                    raise RuntimeError(
                        "Injected storage overlaps a sandbox system root"
                    )
            binding = self._command(
                "port", self.container_id, f"{DEFAULT_QWEN_AGENT_PORT}/tcp"
            ).splitlines()[0]
            if not binding.startswith("127.0.0.1:"):
                raise RuntimeError("The Qwen bridge must be bound to loopback")
            self.metadata = {
                "image_id": image["Id"],
                "labels": labels,
                "platform_mount_count": len(actual.get("Mounts", [])),
                "setup_security_options": host_config["SecurityOpt"],
                "setup_system_paths_unmasked": True,
            }
            return f"ws://{binding}/control"
        except BaseException:
            self.close()
            raise

    def close(self):
        """Kill the owned process tree, including pending native backend work."""
        if self.container_id is not None:
            try:
                if self.diagnostics_path:
                    # Logs precede removal; never persist native settings or keys.
                    result = subprocess.run(
                        ["docker", "logs", self.container_id],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    content = result.stdout + result.stderr
                    content = self.redact(content)
                    self.diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(
                        self.diagnostics_path,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "w") as stream:
                        stream.write(content)
            finally:
                self._command("rm", "--force", self.container_id, missing_ok=True)
                self.container_id = None

    def check_running(self):
        """Report a failed isolation launcher before waiting out a socket timeout."""
        if self.container_id is None:
            raise RuntimeError("The private filesystem launcher is not running")
        state = json.loads(self._command("inspect", self.container_id))[0].get(
            "State", {}
        )
        if not state.get("Running"):
            raise RuntimeError(
                "The private filesystem launcher exited before the Agent was ready; "
                "inspect the private container log with --verbose-logs"
            )

    def redact(self, content: str) -> str:
        """Remove forwarded credentials from private diagnostics."""
        for secret in self._redactions:
            if secret:
                content = content.replace(secret, "[REDACTED]")
        return content

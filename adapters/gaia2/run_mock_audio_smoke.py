"""Offline Docker audio transport check; requires the two built smoke images."""

import argparse
import json
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--qwen-source", type=Path, default=Path("../qwen-audio-agent"))
    parser.add_argument("--qwen-image", default="hearme-qwen-audio:dev")
    parser.add_argument("--are-image", default="hearme-gaia2-are:dev")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    checks = root / "tests/integration/adapters"
    runner = Path(__file__).parent / "qwen_audio_template/environment/runner"
    name = "gaia2-mock-" + uuid4().hex[:12]
    token = secrets.token_urlsafe(32)

    def docker(*parts, **kwargs):
        return subprocess.run(["docker", *parts], check=True, **kwargs)

    docker("network", "create", "--internal", name, stdout=subprocess.DEVNULL)
    try:
        with tempfile.TemporaryDirectory(prefix="gaia2-mock-") as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "token": token,
                        "gatewayOrigin": "http://main:3101",
                        "mcpServers": [],
                    }
                )
            )
            mounts = [
                "-v",
                f"{args.audio.resolve()}:/evaluation/input.wav:ro",
                "-v",
                f"{checks}:/checks:ro",
            ]
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                name,
                "--network-alias",
                "main",
                *mounts,
                "-v",
                f"{args.qwen_source.resolve() / 'server/src'}:/opt/qwen/server/src:ro",
                "-v",
                f"{args.qwen_source.resolve() / 'shared'}:/opt/qwen/shared:ro",
                "-v",
                f"{args.qwen_source.resolve() / 'scripts'}:/opt/qwen/scripts:ro",
                "-v",
                f"{config}:/run/evaluation/config.json:ro",
                "-e",
                "QWAUDIO_EVALUATION_CONFIG=/run/evaluation/config.json",
                "-e",
                "AGENT_PROTOCOL=none",
                "-e",
                "DASHSCOPE_API_KEY=offline-mock-key",
                "-e",
                "QWEN_AUDIO_REALTIME_BASE_URL=ws://127.0.0.1:8199/v1/realtime",
                args.qwen_image,
                "sh",
                "-c",
                "node /checks/mock_realtime.cjs & exec node /opt/qwen/server/src/index.mjs",
                stdout=subprocess.DEVNULL,
            )
            try:
                docker(
                    "run",
                    "--rm",
                    "--network",
                    name,
                    *mounts,
                    "-v",
                    f"{runner.resolve()}:/opt/runner:ro",
                    "-v",
                    f"{args.output_dir.resolve()}:/test-output",
                    "-e",
                    f"QWAUDIO_EVALUATION_TOKEN={token}",
                    args.are_image,
                    "python",
                    "/checks/gateway_audio_smoke.py",
                )
            finally:
                logs = subprocess.run(
                    ["docker", "logs", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                (args.output_dir / "gateway-container.log").write_text(
                    logs.stdout + logs.stderr
                )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        docker("network", "rm", name, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()

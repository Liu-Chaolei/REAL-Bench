"""Build an isolated agent from a clean checkout without changing its files."""

import argparse
import audioop
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import wave
from pathlib import Path


def main():
    """Archive the tested revision, include the bridge, and build the image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qwen_root", type=Path)
    parser.add_argument("--tag", default="qwen-audio-agent:0.5.4-scenario-clock")
    parser.add_argument(
        "--base-image",
        help="Reuse a verified image with identical Qwen source and dependency locks",
    )
    args = parser.parse_args()
    root = args.qwen_root.resolve()

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(root), *arguments])

    if git("status", "--porcelain").strip():
        parser.error("Use a clean Qwen checkout so the built revision is reproducible")
    archive = git("archive", "--format=tar", "HEAD")
    revision = git("rev-parse", "HEAD").decode().strip()
    digest = hashlib.sha256(archive).hexdigest()
    provenance = {"qwen_source_revision": revision, "qwen_source_sha256": digest}
    here = Path(__file__).resolve().parent
    bridge = here / "bridge"
    base = None
    if args.base_image:
        base = json.loads(
            subprocess.check_output(["docker", "image", "inspect", args.base_image])
        )[0]
        labels = base.get("Config", {}).get("Labels") or {}
        required = {
            "org.tau.qwen-agent.source-revision": revision,
            "org.tau.qwen-agent.source-sha256": digest,
            "org.tau.qwen-agent.protocol": "1",
            "org.tau.qwen-agent.confinement": "mount-namespace-v1",
            "org.tau.qwen-agent.domains": "retail,gaia2",
            "org.tau.speech-to-speech": "1.1.0",
            "org.tau.speech-llm-fallback": "1",
        }
        if any(labels.get(key) != value for key, value in required.items()):
            parser.error(
                "The base image must match the Qwen source and runtime profile"
            )
        locks = {
            "/opt/s2s-requirements.lock": bridge / "speech/requirements.lock",
            "/opt/tau-bridge/package-lock.json": bridge / "package-lock.json",
        }
        checksums = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sha256sum",
                args.base_image,
                *locks,
            ],
            text=True,
        )
        expected = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in locks.items()
        }
        actual = dict(reversed(line.split()) for line in checksums.splitlines())
        if actual != expected:
            parser.error("The base image's dependency locks differ; use a full build")
        print(f"Reusing verified image {base['Id']}", flush=True)
    with tempfile.TemporaryDirectory(prefix="tau-qwen-image-") as temporary:
        context = Path(temporary)
        source = context / "qwen-source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(source, filter="data")
        # No .env or gitignored state enters the image, including nested examples.
        for path in source.rglob(".env*"):
            if path.is_file():
                path.unlink()
        shutil.copytree(
            bridge,
            context / "bridge",
            ignore=shutil.ignore_patterns("node_modules", "__pycache__", "*.pyc"),
        )
        shutil.copy(
            here / ("Dockerfile.frontend" if base else "Dockerfile"),
            context / "Dockerfile",
        )
        fixtures = context / "bridge/test/fixtures"
        fixtures.mkdir(exist_ok=True)
        public_audio = here / "testdata"
        # Generic speech drives the real VAD; remote ASR replies are stubbed.
        for name, filename in [
            ("order", "check_order_12345.wav"),
            ("consent", "hi_how_are_you.wav"),
        ]:
            with wave.open(str(public_audio / filename)) as reader:
                pcm = reader.readframes(reader.getnframes())
                if reader.getsampwidth() != 2 or reader.getnchannels() != 1:
                    raise ValueError("Expected public mono PCM16 test audio")
                if reader.getframerate() != 16000:
                    pcm, _ = audioop.ratecv(
                        pcm, 2, 1, reader.getframerate(), 16000, None
                    )
            (fixtures / f"{name}.pcm").write_bytes(pcm)
        (context / "provenance.json").write_text(json.dumps(provenance))
        if base is None:
            checked_in_assets = here / "speech-assets"
            if checked_in_assets.is_dir():
                shutil.copytree(checked_in_assets, context / "speech-assets")
            else:
                subprocess.run(
                    [
                        sys.executable,
                        str(bridge / "speech/prepare_assets.py"),
                        str(context / "speech-assets"),
                    ],
                    check=True,
                )
        subprocess.run(
            [
                "docker",
                "build",
                "--iidfile",
                str(context / "image-id"),
                "--tag",
                args.tag,
                "--build-arg",
                f"QWEN_SOURCE_REVISION={revision}",
                "--build-arg",
                f"QWEN_SOURCE_SHA256={digest}",
                *(
                    [
                        "--build-arg",
                        f"BASE_IMAGE={args.base_image}",
                        "--build-arg",
                        f"BASE_IMAGE_ID={base['Id']}",
                    ]
                    if base
                    else []
                ),
                str(context),
            ],
            check=True,
        )
        image_id_file = context / "image-id"
        if not image_id_file.exists() or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", image_id_file.read_text().strip()
        ):
            raise RuntimeError("Docker build did not produce a verified image ID")
        tagged = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", args.tag], text=True
        ).strip()
        if tagged != image_id_file.read_text().strip():
            raise RuntimeError("Docker tag does not identify the image just built")
        if base:
            built = json.loads(
                subprocess.check_output(["docker", "image", "inspect", tagged])
            )[0]
            layers = base["RootFS"]["Layers"]
            if built["RootFS"]["Layers"][: len(layers)] != layers:
                raise RuntimeError("The base image changed during the frontend build")
        # Namespace setup needs runtime security options that Docker build RUN
        # does not provide. Verify the complete image under actual platform UID.
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--security-opt",
                "seccomp=unconfined",
                "--security-opt",
                "apparmor=unconfined",
                "--security-opt",
                "systempaths=unconfined",
                "--env",
                "TAU_QWEN_TEST_ROOT=/opt/qwen-audio-agent",
                "--env",
                "TAU_QWEN_CODE_TEST=1",
                "--env",
                "TAU_QWEN_SYSTEM_TEST=1",
                "--entrypoint",
                "node",
                args.tag,
                "/opt/tau-bridge/test/verify-image.mjs",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        print(result.stdout, end="")
        if result.returncode or "TAU_IMAGE_VERIFIED" not in result.stdout:
            raise RuntimeError("Image verification failed: " + result.stderr)


if __name__ == "__main__":
    main()

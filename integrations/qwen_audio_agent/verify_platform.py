"""Key-free verification under the real platform UID and automatic mounts."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

IMAGE = "docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.3.1"


def main():
    """Create owned shared markers, prove visibility, then prove confinement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=IMAGE)
    args = parser.parse_args()
    # The repository is on the platform shared volume, unlike host /tmp.
    here = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix=".platform-probe-", dir=here) as temp:
        marker = Path(temp) / "marker"
        marker.write_text("tau-platform-test-marker")
        paths = [str(marker)]
        if str(marker).startswith("/aistor/hpc_stor03/"):
            paths.append(str(marker).removeprefix("/aistor"))
        elif str(marker).startswith("/hpc_stor03/"):
            paths.append("/aistor" + str(marker))
        paths = [path for path in paths if Path(path).exists()]
        env = dict(os.environ, TAU_SANDBOX_TEST_PATHS=json.dumps(paths))
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
                "TAU_SANDBOX_TEST_PATHS",
                "--entrypoint",
                "node",
                args.image,
                "/opt/tau-bridge/test/platform.mjs",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        print(result.stdout, end="")
        if "TAU_PLATFORM_VERIFIED" not in result.stdout:
            raise RuntimeError(
                "Platform confinement verification failed: " + result.stderr
            )


if __name__ == "__main__":
    main()

"""Run one SAASR JSONL worker in an owned, ordinary GPU container."""

import argparse
import os
import re
import signal
import subprocess
from pathlib import Path


def main():
    """Mount explicit synthesis inputs/output and stop only this invocation's container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--worker-directory", type=Path, required=True)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    source, model, output, work = [
        p.resolve()
        for p in (args.source, args.model, args.output_root, args.worker_directory)
    ]
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    cidfile = work / "container.cid"
    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--network",
        "none",
        "--cidfile",
        str(cidfile),
        "--gpus",
        f"device={args.gpu}",
        "--volume",
        f"{source}:{source}:ro",
        "--volume",
        f"{model}:{model}:ro",
        "--volume",
        f"{output}:{output}",
        "--volume",
        f"{work}:{work}",
        "--env",
        f"PYTHONPATH={source / 'src'}",
        "--env",
        "HF_HUB_OFFLINE=1",
        "--env",
        "TRANSFORMERS_OFFLINE=1",
        "--env",
        "OMP_NUM_THREADS=1",
        args.image,
        "/usr/bin/python3",
        "-m",
        "saasr_mini.worker",
    ]
    child = None
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        if cidfile.exists():
            identifier = cidfile.read_text().strip()
            if re.fullmatch(r"[a-f0-9]{64}", identifier):
                subprocess.run(
                    ["docker", "stop", "--time", "2", identifier],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        child = subprocess.Popen(
            command, stdin=None, stdout=None, stderr=None, env=dict(os.environ)
        )
        return child.wait()
    finally:
        stop()


if __name__ == "__main__":
    raise SystemExit(main())

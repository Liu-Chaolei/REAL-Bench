"""Measure secondary MiniMax API concurrency without changing authored tasks."""

import argparse
import json
import math
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    """Probe 32 then double, or halve after failure; persist measured outcomes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-env", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pause-pid", type=int)
    parser.add_argument("--max-concurrency", type=int, default=128)
    parser.add_argument("--start-concurrency", type=int, default=32)
    parser.add_argument("--api-key-env", default="MINIMAX_API_KEY2")
    parser.add_argument("--base-url-env", default="MINIMAX_BASE_URL2")
    parser.add_argument("--model-env", default="GAIA2_BACKEND_MODEL")
    parser.add_argument("--confirm-waves", type=int, default=1)
    args = parser.parse_args()
    values = dict(dotenv_values(args.config_env))
    values.update(os.environ)
    base = values[args.base_url_env].rstrip("/")
    key = values[args.api_key_env]
    model = values[args.model_env]
    tasks = json.loads((args.source / "tasks.json").read_text())
    report = {
        "endpoint": base,
        "model": model,
        "kind": "short_request_capacity_probe",
        "waves": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def wave(concurrency):
        barrier = threading.Barrier(concurrency)
        with httpx.Client(
            timeout=httpx.Timeout(90, connect=15),
            trust_env=False,
            limits=httpx.Limits(
                max_connections=concurrency, max_keepalive_connections=concurrency
            ),
        ) as client:

            def request(index):
                task = tasks[index % len(tasks)]
                original = next(
                    e["arguments"].get("content", "")
                    for e in task["scenario"]["events"]
                    if e["kind"] == "user"
                )
                payload = {
                    "model": model,
                    "max_tokens": 512,
                    "reasoning_effort": "low",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "This is an API capacity test. Read this benchmark request and reply with "
                                "one brief sentence naming its main goal; do not execute any actions.\n"
                                + original
                            ),
                        }
                    ],
                }
                barrier.wait()
                start = time.monotonic()
                try:
                    response = client.post(
                        base + "/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                    )
                    data = response.json()
                    choices = data.get("choices", [])
                    ok = response.status_code == 200 and bool(choices)
                    return {
                        "ok": ok,
                        "http_status": response.status_code,
                        "seconds": time.monotonic() - start,
                        "usage": data.get("usage"),
                        "error_code": data.get("error", {}).get("code")
                        if isinstance(data.get("error"), dict)
                        else None,
                    }
                except Exception as error:
                    return {
                        "ok": False,
                        "error_type": type(error).__name__,
                        "seconds": time.monotonic() - start,
                    }

            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(request, range(concurrency)))
            elapsed = time.monotonic() - start
        durations = sorted(result["seconds"] for result in results)
        succeeded = sum(result["ok"] for result in results)
        result = {
            "concurrency": concurrency,
            "successes": succeeded,
            "failures": concurrency - succeeded,
            "elapsed_seconds": elapsed,
            "p50_seconds": durations[len(durations) // 2],
            "p95_seconds": durations[math.ceil(0.95 * len(durations)) - 1],
            "successful_requests_per_minute": succeeded / elapsed * 60,
            "requests": results,
        }
        result["healthy"] = (
            succeeded / concurrency >= 0.95 and result["p95_seconds"] < 60
        )
        report["waves"].append(result)
        save()
        print(
            json.dumps({k: v for k, v in result.items() if k != "requests"}), flush=True
        )
        return result

    if args.pause_pid:
        os.kill(args.pause_pid, signal.SIGSTOP)
    try:
        current = wave(args.start_concurrency)
        for _ in range(args.confirm_waves - 1):
            if not current["healthy"]:
                break
            current = wave(args.start_concurrency)
        best = current if current["healthy"] else None
        if best:
            for concurrency in [args.start_concurrency * 2, args.start_concurrency * 4]:
                if concurrency > args.max_concurrency:
                    break
                candidate = wave(concurrency)
                if not candidate["healthy"]:
                    break
                if (
                    candidate["successful_requests_per_minute"]
                    <= best["successful_requests_per_minute"] * 1.15
                ):
                    break
                best = candidate
        else:
            for concurrency in [
                args.start_concurrency // 2**power
                for power in range(1, 4)
                if args.start_concurrency // 2**power >= 1
            ]:
                candidate = wave(concurrency)
                for _ in range(args.confirm_waves - 1):
                    if not candidate["healthy"]:
                        break
                    candidate = wave(concurrency)
                if candidate["healthy"]:
                    best = candidate
                    break
        report["recommended_concurrency"] = best["concurrency"] if best else None
        report["completed"] = True
        save()
    finally:
        if args.pause_pid:
            os.kill(args.pause_pid, signal.SIGCONT)


if __name__ == "__main__":
    main()

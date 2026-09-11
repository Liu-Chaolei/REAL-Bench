"""Drive a real Gateway with an external WAV and a mocked speech provider."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, "/opt/runner")
from audio_driver import AudioDriver
from boundary import BoundaryAssessor


async def main():
    async with httpx.AsyncClient(trust_env=False) as http:
        for _ in range(120):
            try:
                response = await http.get(
                    "http://main:3101/api/health",
                    headers={
                        "Authorization": "Bearer "
                        + os.environ["QWAUDIO_EVALUATION_TOKEN"],
                    },
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Mock Gateway startup failed")
    messages = [{"type": "USER_MESSAGE", "message": "REFERENCE TEXT MUST NOT BE SENT"}]

    def notifications():
        current = list(messages)
        messages.clear()
        return current

    replies = []
    output = Path("/test-output")
    driver = AudioDriver(
        "http://main:3101",
        os.environ["QWAUDIO_EVALUATION_TOKEN"],
        Path("/evaluation/input.wav"),
        output,
        notifications,
        lambda: (1728975600, 0),
        replies.append,
        assessor=BoundaryAssessor(
            lambda messages: (
                '{"kind":"final","acts":["answer"],"reason":"known transport fixture"}',
                None,
            )
        ),
        timeout=90,
        idle_seconds=1,
    )

    async def submit_backend_result():
        while not driver.input_done:
            await asyncio.sleep(0.05)
        await driver.deliver("transport-result", "final", "Audio transport verified.")

    async with asyncio.TaskGroup() as group:
        group.create_task(submit_backend_result())
        await driver.run()
    if driver.recoveries != 1:
        raise RuntimeError(
            f"Expected one recovered idle closure; got {driver.recoveries}"
        )
    if replies != ["Audio transport verified."]:
        raise RuntimeError(f"Unexpected played replies: {replies}")
    timeline = (output / "gateway.jsonl").read_text()
    if (
        "REFERENCE TEXT MUST NOT BE SENT" in timeline
        or '"type": "text.message"' in timeline
    ):
        raise RuntimeError("Reference text leaked into the input transport")
    print(
        json.dumps(
            {
                "mock_frontend": True,
                "real_gateway": True,
                "real_wav": True,
                "played_replies": len(replies),
                "paid_model_calls": 0,
                "recovered_idle_closures": driver.recoveries,
            }
        )
    )


asyncio.run(main())

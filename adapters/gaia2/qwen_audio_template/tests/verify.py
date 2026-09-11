"""Read only evidence collected from the trusted ARE service."""

import json
from pathlib import Path

result = json.loads(Path("/evaluation/results/result.json").read_text())
if result.get("measurement_valid") is False:
    raise RuntimeError("Measurement is invalid; no task reward may be emitted")
if result["status"] not in {"completed", "execution_timeout", "protocol_failure"}:
    raise RuntimeError(f"GAIA2 infrastructure error: {result}")
if result["status"] == "completed" and not isinstance(result.get("success"), bool):
    raise ValueError("Missing official success decision")
reward = 1 if result["status"] == "completed" and result["success"] else 0
Path("/logs/verifier/reward.txt").write_text(str(reward))
Path("/logs/verifier/gaia2_qwen_audio_result.json").write_text(
    json.dumps(result, indent=2)
)

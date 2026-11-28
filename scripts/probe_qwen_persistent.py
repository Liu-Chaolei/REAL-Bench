"""Probe a real Qwen image's passive native observer without submitting tasks."""

import argparse
import asyncio
import json
from pathlib import Path

from tau2.adapters.qwen_audio_agent.config import QwenAgentConfig
from tau2.adapters.qwen_audio_agent.provider import QwenAgentProvider


async def probe(config, output):
    provider = QwenAgentProvider(config)
    provider.persistent_tasks_enabled = True
    report = {"status": "not_connected", "capabilities": None, "verified_scenarios": 0}
    try:
        await provider.connect(
            "Serve the caller in English using your native task capabilities.", []
        )
        report.update(
            status="observer_ready",
            capabilities=provider.backend_capabilities,
            image_id=provider.metadata.get("image_id"),
            source_revision=provider.metadata.get("qwen_source_revision"),
        )
    except Exception as error:
        report.update(
            status="connection_failed",
            error_type=type(error).__name__,
            error=provider.runtime.redact(str(error)),
        )
    finally:
        await provider.disconnect()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch(mode=0o600)
        output.chmod(0o600)
        output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return report["status"] == "observer_ready"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = QwenAgentConfig.model_validate_json(args.config.read_text()).model_copy(
        update={"domain": "gaia2"}
    )
    raise SystemExit(0 if asyncio.run(probe(config, args.output)) else 1)


if __name__ == "__main__":
    main()

"""V7 preserves native observation and proves only speech-independent failures."""

from __future__ import annotations

import json

try:
    from typing import override
except ImportError:
    from typing_extensions import override

from action_decision import VERSION, action_count_proof, apply_decision
from passive_driver import PassiveDriver, PassiveProtocol
from playback_timeline import build_timeline


class CountProtocol(PassiveProtocol):
    version = VERSION


class InterruptionProtocol(CountProtocol):
    version = "gaia2-qwen-audio/8"


class CountDriver(PassiveDriver):
    def __init__(self, *args, scenario, source_text, **kwargs):
        super().__init__(*args, **kwargs)
        self.scenario = scenario
        self.source_text = source_text

    @override
    async def run(self):
        try:
            await super().run()
        except* TimeoutError:
            # The task budget closed observation; speech uncertainty is not a zero.
            if not self.task_evidence or self.task_evidence.get("score_basis") not in {
                "official_action_count_failure",
                "unresolved_speech",
            }:
                raise

    @override
    def final_evidence(self):
        evidence = super().final_evidence()
        if self.contract["protocol"] == "gaia2-qwen-audio/8":
            from interruption_evidence import build_interruption_evidence

            report = build_interruption_evidence(self.output)
            (self.output / "interruption-evidence.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
        if evidence["task_score_status"] == "invalid_execution":
            return evidence
        if self.observation.end_reason not in {"idle_window", "total_timeout"}:
            raise ValueError(
                "Action evidence must be collected after observation closes"
            )
        timeline = build_timeline(
            self.output,
            include_interruptions=self.contract["protocol"] == "gaia2-qwen-audio/8",
        )
        (self.output / "playback-timeline.json").write_text(
            json.dumps(timeline, indent=2) + "\n"
        )
        if evidence["task_score_status"] == "unresolved_speech":
            proof = action_count_proof(
                self.scenario,
                self.runtime.env.event_log.list_view(),
                self.contract,
                self.source_text,
            )
            (self.output / "decision-proof.json").write_text(
                json.dumps(proof, indent=2) + "\n"
            )
            evidence = apply_decision(evidence, proof)
        return evidence

"""V5 observation driver with independent task and speech evidence states."""

from __future__ import annotations

import json
try:
    from typing import override
except ImportError:
    from typing_extensions import override

from boundary import MeasurementError
from evidence import evaluate_evidence
from task_contract import VERSION
from window_driver import WindowDriver, WindowProtocol


class InvalidTaskEvidence(MeasurementError):
    cause = "missing_or_corrupt_evidence"


class UnverifiedInterruption(InvalidTaskEvidence):
    cause = "unverified_interruption"


class TaskProtocol(WindowProtocol):
    version = VERSION


class TaskDriver(WindowDriver):
    def __init__(self, *args, contract, **kwargs):
        super().__init__(*args, **kwargs)
        self.contract = contract
        self.task_evidence = None

    def final_evidence(self):
        return evaluate_evidence(
            self.output, self.contract, end_reason=self.observation.end_reason
        )

    @override
    def finalize_projection(self):
        evidence = self.final_evidence()
        self.task_evidence = evidence
        self.observation.audible_complete = evidence["audible_transcript_complete"]
        evidence["observation_end_sec"] = self.observation.end_sec
        (self.output / "projection.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        )
        if evidence["task_score_status"] == "invalid_execution":
            if evidence["speech_evidence_status"] == "unverified_interruption":
                raise UnverifiedInterruption(
                    "Cancellation evidence could not be associated with the played response"
                )
            raise InvalidTaskEvidence("Recorded speech is missing or inconsistent")
        text = evidence["task_submission_text"]
        if text:
            with self.runtime.tick_lock:
                self.reply(text)
            self.protocol.turn = 1
            self.protocol.record(
                "observation.submitted",
                text_sha256=evidence["task_submission_sha256"],
                are_turn=1,
                segment_count=len(evidence["complete_segments"]),
            )
        self.protocol.record(
            "task.evidence_checked",
            task_score_status=evidence["task_score_status"],
            speech_evidence_status=evidence["speech_evidence_status"],
        )

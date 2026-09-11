"""Independent interaction evidence. Never changes an official task reward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_rows(path):
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def interaction_report(root: Path):
    rows = read_rows(root / "protocol.jsonl")
    gateway = read_rows(root / "gateway.jsonl")
    requested = {
        r["delivery_id"]: r for r in rows if r["event"] == "delivery.requested"
    }
    assessed = {r["delivery_id"]: r for r in rows if r["event"] == "delivery.assessed"}
    responses = []
    findings = []
    for row in rows:
        if row["event"] not in {"delivery.committed", "delivery.played"}:
            continue
        identity = row["delivery_id"]
        request = requested.get(identity, {})
        assessment = assessed.get(identity, {})
        acts = sorted(
            {
                act
                for vote in assessment.get("evidence", {}).get("votes", [])
                for act in vote.get("acts", [])
            }
        )
        response = {
            "delivery_id": identity,
            "response_id": row.get("response_id"),
            "spoken": row["transcript"],
            "kind": row.get("kind"),
            "declared_kind": row.get("declared_kind"),
            "acts": acts,
            "requested_content": request.get("content"),
            "boundary_evidence": assessment.get("evidence"),
            "review": {
                "audio_matches_transcript": None,
                "capability_claim_correct": None,
                "result_preserved": None,
                "repetition_appropriate": None,
            },
        }
        responses.append(response)
        if (
            len(
                {v.get("kind") for v in assessment.get("evidence", {}).get("votes", [])}
            )
            > 1
        ):
            findings.append(
                {
                    "type": "formal_subtype_disagreement_requires_review",
                    "delivery_id": identity,
                }
            )
        if request and request["content"].strip() != row["transcript"].strip():
            findings.append(
                {
                    "type": "text_difference_requires_semantic_review",
                    "delivery_id": identity,
                    "note": "Paraphrases may be correct; this is not an automatic failure.",
                }
            )
        if "capability_claim" in acts:
            findings.append(
                {"type": "capability_claim_requires_review", "delivery_id": identity}
            )
        if "Request state declared" in row["transcript"]:
            findings.append(
                {"type": "internal_placeholder_spoken", "delivery_id": identity}
            )
    audio = []
    for path in sorted(root.glob("response-*.json")):
        data = json.loads(path.read_text())
        audio.append(
            {
                **data,
                "played_audio": path.with_suffix(".wav").name,
                "generated_audio": path.with_name(path.stem + ".generated.wav").name,
            }
        )
        if not data.get("played_completely") and data.get("played_samples", 0):
            findings.append(
                {
                    "type": "partial_audio_requires_listening",
                    "response_id": data["response_id"],
                }
            )
    windowed = any(r.get("protocol") == "gaia2-qwen-audio/4" for r in rows)
    if windowed:
        for response in responses:
            response.pop("review", None)
        for finding in findings:
            finding["type"] = (
                finding["type"]
                .replace(
                    "_requires_semantic_review", "_semantic_comparison_not_performed"
                )
                .replace("_requires_listening", "_without_verified_transcript")
                .replace("_requires_review", "_not_semantically_assessed")
            )
    return {
        "metric": "interaction_quality",
        "automatically_changes_task_score": False,
        "assessment_status": "deterministic_evidence_only"
        if windowed
        else "human_review_required",
        "semantic_assessment": "not_performed" if windowed else "not_human_reviewed",
        "user_segments": [r for r in rows if r["event"] == "input.segment"],
        "application_events": [r for r in rows if r["event"] == "application.result"],
        "provider_inputs": [
            r for r in gateway if r.get("type") == "evaluation.provider_request"
        ],
        "responses": responses,
        "audio": audio,
        "findings": findings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(interaction_report(args.results), ensure_ascii=False, indent=2)
        + "\n"
    )


if __name__ == "__main__":
    main()

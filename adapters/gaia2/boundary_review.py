"""Prepare unlabeled speech examples and check assessor stability automatically.

No human labels or oracle answers are used. Stability is not semantic accuracy.
The legacy calibration command/path names remain for compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from adapters.gaia2.qwen_audio_template.environment.runner.boundary import (
    PROMPT,
    PROMPT_VERSION,
    BoundaryAssessor,
)
from adapters.gaia2.qwen_audio_template.environment.runner.responses_judge import (
    ResponsesJudge,
)


def prepare(jobs, output, count=60):
    if output.exists():
        raise ValueError("Preserve existing review labels")
    candidates = []
    for path in sorted(jobs.glob("*/artifacts/evaluation/results/gateway.jsonl")):
        users, spoken, seen = [], [], set()
        metadata = {
            json.loads(p.read_text())["response_id"]: p
            for p in path.parent.glob("response-*.json")
        }
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("type") != "transcript.final":
                continue
            if row.get("role") == "user":
                users.append(row.get("content", "")[:1000])
                users = users[-10:]
                continue
            identity = row.get("responseId")
            if not identity or identity in seen or identity not in metadata:
                continue
            info = json.loads(metadata[identity].read_text())
            if not info.get("played_completely"):
                continue
            seen.add(identity)
            text = row.get("content", "")
            if text.strip():
                candidates.append(
                    {
                        "id": path.parents[3].name + ":" + identity,
                        "spoken": text,
                        "user_context": list(users),
                        "spoken_context": list(spoken),
                        "audio": str(metadata[identity].with_suffix(".wav").resolve()),
                        "audio_sha256": hashlib.sha256(
                            metadata[identity].with_suffix(".wav").read_bytes()
                        ).hexdigest(),
                        "notes": "",
                    }
                )
                spoken.append(text)
                spoken = spoken[-10:]
    random.Random(20260909).shuffle(candidates)
    if len(candidates) < count:
        raise ValueError(f"Need {count} played responses; found {len(candidates)}")
    selected = candidates[:count]
    for index, row in enumerate(selected):
        row["split"] = "calibration" if index < count // 2 else "blind"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "seed": 20260909,
                "oracle_visible": False,
                "instructions": "Unlabeled automatic stability check. Audio hashes verify artifact integrity only; audio/transcript semantic equivalence is not verified.",
                "examples": selected,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    return len(selected)


def validate_examples(data):
    examples = data.get("examples", [])
    if len(examples) < 60 or len({r["id"] for r in examples}) != len(examples):
        raise ValueError("Need at least 60 uniquely identified examples")
    if data.get("oracle_visible") is not False:
        raise ValueError("Boundary labels must be reference-blind")
    for row in examples:
        if not isinstance(row.get("spoken"), str) or not row["spoken"].strip():
            raise ValueError("Missing spoken transcript")
        for key in ("user_context", "spoken_context"):
            if not isinstance(row.get(key), list) or any(
                not isinstance(value, str) for value in row[key]
            ):
                raise ValueError("Invalid conversation context")
        audio = Path(row.get("audio", ""))
        if not audio.is_file() or hashlib.sha256(
            audio.read_bytes()
        ).hexdigest() != row.get("audio_sha256"):
            raise ValueError("Recorded audio is missing or changed")
    if sum(r.get("split") == "blind" for r in examples) < 30:
        raise ValueError("Need at least 30 blinded examples")
    return examples


def calibrate(examples_path, output, env_file, services_restored):
    if not services_restored:
        raise ValueError("Services remain unverified; no API calls were made")
    data = json.loads(examples_path.read_text())
    examples = validate_examples(data)
    if output.exists():
        raise ValueError("Preserve previous calibration outputs")
    from dotenv import dotenv_values

    values = dotenv_values(env_file)
    output.mkdir(parents=True)
    model = values.get("GAIA2_JUDGE_MODEL")
    endpoint = values.get("GAIA2_JUDGE_ENDPOINT")
    api_key = values.get("GAIA2_JUDGE_API_KEY") or values.get("OPENAI_API_KEY")
    if not model or not endpoint or not api_key:
        raise ValueError("Missing judge model, endpoint or credential")
    engine = ResponsesJudge(
        model,
        endpoint,
        api_key,
        values.get("GAIA2_JUDGE_REASONING_EFFORT") or "low",
        record_path=output / "calls.jsonl",
    )
    assessor = BoundaryAssessor(engine)
    observations = []
    try:
        for row in examples:
            decision = assessor.classify(
                row["spoken"],
                user_context=row["user_context"],
                spoken_context=row["spoken_context"],
            )
            observation = {
                "id": row["id"],
                "actual": decision.kind,
                "split": row["split"],
                "decision": decision.to_dict(),
                "exact_vote_agreement": decision.votes[0]["kind"]
                == decision.votes[1]["kind"],
            }
            observations.append(observation)
            with (output / "observations.jsonl").open("a") as stream:
                stream.write(json.dumps(observation, ensure_ascii=False) + "\n")
    finally:
        engine.close()
    report = {
        "passed": True,  # Every example resolved; classify raises on boundary uncertainty.
        "validation_mode": "automatic_stability",
        "human_validated": False,
        "semantic_accuracy": None,
        "audio_transcript_semantics_verified": False,
        "examples": len(examples),
        "resolved_examples": len(observations),
        "exact_vote_agreement": sum(r["exact_vote_agreement"] for r in observations)
        / len(observations),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "model": values["GAIA2_JUDGE_MODEL"],
        "effort": values.get("GAIA2_JUDGE_REASONING_EFFORT") or "low",
        "labels_path": str(examples_path.resolve()),
        "labels_sha256": hashlib.sha256(examples_path.read_bytes()).hexdigest(),
        "calls_sha256": hashlib.sha256(
            (output / "calls.jsonl").read_bytes()
        ).hexdigest(),
        "model_calls": engine.calls,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def verify_calibration(campaign, profile):
    """Reproduce calibration from recorded calls; never performs inference."""
    folder = campaign / "boundary-calibration"
    report = json.loads((folder / "summary.json").read_text())
    labels_path = Path(report["labels_path"])
    calls_path = folder / "calls.jsonl"
    if (
        hashlib.sha256(labels_path.read_bytes()).hexdigest() != report["labels_sha256"]
        or hashlib.sha256(calls_path.read_bytes()).hexdigest() != report["calls_sha256"]
    ):
        raise ValueError("Boundary calibration evidence changed")
    examples = validate_examples(json.loads(labels_path.read_text()))
    replay = ResponsesJudge(
        profile["judge_model"], "", "", profile["judge_effort"], replay_path=calls_path
    )
    assessor = BoundaryAssessor(replay)
    total = 0
    try:
        for row in examples:
            assessor.classify(
                row["spoken"],
                user_context=row["user_context"],
                spoken_context=row["spoken_context"],
            )
            total += 1
        if replay.replay is None or replay.calls != len(replay.replay):
            raise ValueError("Calibration replay did not consume the exact evidence")
        if (
            report.get("validation_mode") != "automatic_stability"
            or report.get("human_validated") is not False
            or report.get("audio_transcript_semantics_verified") is not False
            or report.get("semantic_accuracy") is not None
            or report.get("passed") is not True
            or total != report.get("examples")
            or total != report.get("resolved_examples")
            or report.get("model") != profile["judge_model"]
            or report.get("effort") != profile["judge_effort"]
            or report.get("prompt_version") != PROMPT_VERSION
            or report.get("prompt_sha256")
            != hashlib.sha256(PROMPT.encode()).hexdigest()
        ):
            raise ValueError("Recorded automatic boundary check does not pass")
    finally:
        replay.close()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--examples", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--services-restored", action="store_true")
    args = parser.parse_args()
    if args.jobs:
        print("Prepared examples:", prepare(args.jobs, args.output))
    elif args.examples and args.env_file:
        report = calibrate(
            args.examples, args.output, args.env_file, args.services_restored
        )
        print(json.dumps(report))
        if not report["passed"]:
            raise SystemExit(1)
    else:
        parser.error(
            "Supply --jobs for preparation, or --examples and --env-file for calibration"
        )


if __name__ == "__main__":
    main()

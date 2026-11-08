"""Independent two-stage feedback evaluation over sealed delivered evidence."""

import json
import unicodedata
from pathlib import Path

from tau2.diagnostics.conciseness import VERSION
from tau2.diagnostics.conciseness.evidence import build_windows, generation_input
from tau2.diagnostics.conciseness.models import Alignment, NuggetInventory
from tau2.diagnostics.conciseness.scoring import (
    response_length,
    score_window,
    trial_summary,
)
from tau2.diagnostics.intent.io import canonical, digest, write_json
from tau2.diagnostics.model import TauJsonModel


def validate_inventory(raw, sources):
    """Reject invented source references; collapse exact duplicate propositions."""
    value = NuggetInventory.model_validate_json(raw).model_dump(mode="json")
    seen = {}
    for nugget in value["nuggets"]:
        if not set(nugget["evidence_refs"]) <= sources.keys():
            raise ValueError("Unknown nugget evidence reference")
        if not any(sources[r]["kind"] == "user" for r in nugget["evidence_refs"]):
            raise ValueError("Nugget must cite a user requirement")
        if nugget["kind"] in {"result", "reason"} and not any(
            sources[r]["kind"] == "operation" for r in nugget["evidence_refs"]
        ):
            raise ValueError("Business results/causes require execution evidence")
        normalized = " ".join(
            unicodedata.normalize("NFC", nugget["proposition"]).casefold().split()
        )
        if normalized in seen:
            nugget["equivalence_group"] = seen[normalized]
        seen[normalized] = nugget["equivalence_group"]
    return value


def validate_alignment(raw, inventory, window):
    """Enforce frozen coverage; invalid quotes become explicit uncertainty."""
    value = Alignment.model_validate_json(raw).model_dump(mode="json")
    expected = {n["id"] for n in inventory["nuggets"]}
    actual = [m["nugget_id"] for m in value["matches"]]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError("Alignment must cover every frozen nugget exactly once")
    replies = {r["id"]: r for r in window["replies"]}

    def valid_quotes(quotes):
        return bool(quotes) and all(
            q["utterance_id"] in replies
            and q["text"] in replies[q["utterance_id"]]["text"]
            for q in quotes
        )

    validation = []
    nuggets = {n["id"]: n for n in inventory["nuggets"]}
    for match in value["matches"]:
        if match["status"] in {"matched", "contradicted"} and not valid_quotes(
            match["quotes"]
        ):
            validation.append(
                {
                    "nugget_id": match["nugget_id"],
                    "original_status": match["status"],
                    "reason": "invalid_or_missing_quote",
                }
            )
            match.update(status="uncertain", reason="invalid_or_missing_quote")
        if match["status"] == "matched":
            required_times = [
                window["sources"][ref].get("completed_media")
                for ref in nuggets[match["nugget_id"]]["evidence_refs"]
            ]
            known = [time for time in required_times if time is not None]
            if known and all(
                replies[q["utterance_id"]]["started"] < max(known)
                for q in match["quotes"]
            ):
                validation.append(
                    {
                        "nugget_id": match["nugget_id"],
                        "original_status": "matched",
                        "reason": "result_not_available_at_cited_reply_start",
                    }
                )
                match.update(
                    status="uncertain",
                    reason="result_not_available_at_cited_reply_start",
                )
    for finding in value["findings"]:
        sources_valid = set(finding["evidence_refs"]) <= window["sources"].keys()
        material_supported = finding["kind"] != "material_error" or bool(
            finding["evidence_refs"]
        )
        if (
            not valid_quotes(finding["quotes"])
            or not sources_valid
            or not material_supported
        ):
            validation.append(
                {"original_kind": finding["kind"], "reason": "invalid_finding_citation"}
            )
            finding.update(kind="uncertain", reason="invalid_finding_citation")
    # A validated contradiction of a frozen business obligation is itself a
    # substantive error, even if the judge forgot to duplicate it as a finding.
    for match in value["matches"]:
        if match["status"] == "contradicted":
            value["findings"].append(
                {
                    "kind": "material_error",
                    "reason": match["reason"],
                    "quotes": match["quotes"],
                    "evidence_refs": nuggets[match["nugget_id"]]["evidence_refs"],
                }
            )
    if any(f["kind"] == "material_error" for f in value["findings"]):
        value["fidelity_status"] = "material_error"
    elif value["fidelity_status"] == "material_error":
        # A bare label cannot trigger the zero-score gate.
        value["fidelity_status"] = "uncertain"
        validation.append({"reason": "material_error_without_supported_finding"})
    if value["fidelity_status"] != "material_error" and (
        validation
        or any(f["kind"] == "uncertain" for f in value["findings"])
        or any(m["status"] in {"uncertain", "contradicted"} for m in value["matches"])
    ):
        value["fidelity_status"] = "uncertain"
    return value, validation


def _rubric(name):
    return (Path(__file__).parent / "rubrics" / (name + ".md")).read_text()


def _stage(model, prompt, schema, validator, path):
    """Keep successful semantic validation cached; failed requests remain retryable."""
    raw = model.complete_sync(prompt, schema, validate=validator)
    write_json(path, json.loads(raw))
    return validator(raw)


def _audio_stats(evidence, directory, windows):
    if evidence.audio.get("status") != "collected":
        return {
            w["id"]: {"status": "unavailable", "reason": "missing_audio"}
            for w in windows
        }
    try:
        from tau2.diagnostics.voice import evaluate_voice

        report = evaluate_voice(evidence, directory)
        waveform = report.get("waveform_evidence", {})
        if waveform.get("status") != "valid":
            raise ValueError("invalid_waveform_evidence")
        intervals = waveform["agent_intervals"]
        result = {}
        for window in windows:
            lower, upper = window.get("start", 0), window.get("end")
            upper = evidence.audio["duration"] if upper is None else upper
            speech = sum(max(0, min(b, upper) - max(a, lower)) for a, b in intervals)
            from tau2.metrics.real_voice import _intervals

            reply_spans = _intervals(
                [
                    [r["started"], r["ended"]]
                    for r in window.get("replies", [])
                    if r["ended"] > r["started"]
                ]
            )
            playback = sum(
                max(0, min(row["end"], b) - max(row["start"], a))
                for row in evidence.audio["timeline"]
                if row.get("agent")
                for a, b in reply_spans
            )
            result[window["id"]] = {
                "status": "measured",
                "agent_speech_seconds": speech,
                "agent_playback_seconds": playback,
                "playback_basis": "delivered_audio_within_recorded_reply_spans_including_pauses",
                "required_nuggets_per_speech_second": None,
                "time_basis": "actual_media_delivery_and_playback",
            }
        return result
    except Exception as exc:
        return {
            w["id"]: {"status": "unavailable", "reason": type(exc).__name__}
            for w in windows
        }


def evaluate_feedback(evidence, config, directory, output, budget, language=None):
    """Generate obligations without target text, freeze them, then match replies."""
    options = config.conciseness
    language = options.language or language or evidence.source.get("language")
    calibration = options.calibration
    length_budget = (
        calibration.languages.get(language) if calibration and language else None
    )
    windows = build_windows(evidence, options)
    output, directory = Path(output), Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    cache = directory.parent / "conciseness-cache"
    # Stable across other measures and budget calibration changes. Nugget
    # extraction is independent of target replies and of length parameters.
    requests = cache / "requests"
    models = {
        role: TauJsonModel(config, role, requests, budget)
        for role in ("feedback_nuggets", "feedback_alignment")
    }
    history, results = [], []
    for window in windows:
        target = output / window["id"].replace(":", "_")
        write_json(target / "window.json", window)
        result = {k: window.get(k) for k in ("id", "stage", "status", "reason")}
        result["response_length"] = response_length(window.get("replies", []))
        if window["status"] != "ready":
            results.append(result)
            continue
        blind = generation_input(window, history)
        write_json(target / "generation-input.json", blind)
        inventory_schema = NuggetInventory.model_json_schema()
        prompt = _rubric("nuggets") + "\nINPUT:\n" + canonical(blind)
        key = digest(
            {
                "prompt": prompt,
                "schema": inventory_schema,
                "model": config.profile("feedback_nuggets").model_dump(),
                "version": VERSION,
            }
        )
        frozen_path = cache / "nuggets" / (key + ".json")
        try:

            def validator(raw):
                return validate_inventory(raw, window["sources"])

            if frozen_path.exists():
                frozen = json.loads(frozen_path.read_text())
                inventory = validator(canonical(frozen["inventory"]))
                if frozen["input_key"] != key or digest(inventory) != frozen["sha256"]:
                    raise ValueError("Frozen nugget checksum mismatch")
            else:
                inventory = _stage(
                    models["feedback_nuggets"],
                    prompt,
                    inventory_schema,
                    validator,
                    target / "nuggets-raw.json",
                )
                # Only successful inventories freeze. Explicit reruns can retry
                # unavailable generations, but cannot redraw a valid standard.
                if inventory["status"] == "complete":
                    write_json(
                        frozen_path,
                        {
                            "input_key": key,
                            "inventory": inventory,
                            "sha256": digest(inventory),
                        },
                    )
            write_json(target / "nuggets.json", inventory)
            result.update(
                outcome=inventory["outcome"], nuggets_sha256=digest(inventory)
            )
            if inventory["status"] != "complete":
                result.update(status=inventory["status"], reason=inventory["reason"])
                results.append(result)
                continue
            if not window["replies"]:
                alignment = {
                    "matches": [
                        {
                            "nugget_id": n["id"],
                            "status": "missing",
                            "reason": "no_delivered_reply",
                            "quotes": [],
                        }
                        for n in inventory["nuggets"]
                    ],
                    "findings": [],
                    "fidelity_status": "supported",
                    "fidelity_reason": "No delivered claims",
                }
                validations = []
            else:
                payload = {
                    "inventory": inventory,
                    "context": blind,
                    "replies": window["replies"],
                }
                write_json(target / "alignment-input.json", payload)

                def validator(raw):
                    return validate_alignment(raw, inventory, window)

                alignment, validations = _stage(
                    models["feedback_alignment"],
                    _rubric("alignment") + "\nINPUT:\n" + canonical(payload),
                    Alignment.model_json_schema(),
                    validator,
                    target / "alignment-raw.json",
                )
            write_json(target / "alignment.json", alignment)
            write_json(target / "validation.json", validations)
            result.update(
                score_window(inventory, alignment, window["replies"], length_budget)
            )
            matched = {
                m["nugget_id"] for m in alignment["matches"] if m["status"] == "matched"
            }
            if alignment["fidelity_status"] == "supported":
                history.extend(
                    {"window_id": window["id"], "proposition": n["proposition"]}
                    for n in inventory["nuggets"]
                    if n["id"] in matched
                )
        except Exception as exc:
            result.update(status="grader_error", reason=type(exc).__name__)
            write_json(
                target / "error.json",
                {"status": "grader_error", "reason": type(exc).__name__},
            )
        result["directory"] = str(target)
        write_json(target / "scores.json", result)
        results.append(result)
    audio = _audio_stats(evidence, directory, windows)
    for result in results:
        result["audio"] = audio.get(result["id"])
        speech = (result["audio"] or {}).get("agent_speech_seconds")
        if speech:
            result["audio"]["required_nuggets_per_speech_second"] = (
                result.get("required_matched", 0) / speech
                if "required_matched" in result and not result.get("required_uncertain")
                else None
            )
        if result.get("directory"):
            write_json(Path(result["directory"]) / "scores.json", result)
    trial = trial_summary(results)
    complete = bool(results) and all(
        r["status"] in {"measured", "not_applicable"} for r in results
    )
    return {
        "version": VERSION,
        "status": "measured"
        if complete
        else "evidence_incomplete"
        if results
        else "not_applicable",
        "domain": evidence.domain,
        "language": language,
        "evaluation_mode": "request_window_batch",
        "future_context_visible_within_window": True,
        "calibration_id": calibration.id if calibration else None,
        "calibration_sha256": digest(calibration.model_dump()) if calibration else None,
        "diagnostic_only": not evidence.complete,
        "windows": results,
        "trial": trial,
    }

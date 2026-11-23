"""Convert backend-specific SRL frames into stable probe records."""

from __future__ import annotations

from typing import Any

from propbank_probe.models import ArgumentRecord, PredicateRecord, SentenceParse


def _span_text(text: str, offsets: list[tuple[int, int]], begin: int, end: int, fallback: str):
    if offsets and 0 <= begin < len(offsets) and 0 <= end - 1 < len(offsets):
        start = offsets[begin][0]
        finish = offsets[end - 1][1]
        return start, finish, text[start:finish]
    return 0, 0, fallback


def normalize_raw(case_id: str, text: str, raw_result: dict[str, Any]) -> SentenceParse:
    """Normalize one backend result while retaining its raw payload."""

    offsets = [tuple(item) for item in raw_result.get("token_offsets", [])]
    predicates: list[PredicateRecord] = []
    errors: list[dict[str, Any]] = []
    for frame in raw_result.get("frames", []):
        predicate_item = next((item for item in frame if item[1] == "PRED"), None)
        if predicate_item is None:
            errors.append({"reason": "missing_predicate_marker", "frame": frame})
            continue
        predicate_text, _, pred_begin, pred_end = predicate_item
        pred_start, pred_finish, pred_surface = _span_text(
            text, offsets, pred_begin, pred_end, predicate_text
        )
        args: list[ArgumentRecord] = []
        adjuncts: list[ArgumentRecord] = []
        for surface, role, begin, end in frame:
            if role == "PRED":
                continue
            start, finish, source_text = _span_text(text, offsets, begin, end, surface)
            record = ArgumentRecord(
                role=role,
                text=source_text or surface,
                char_start=start,
                char_end=finish,
                token_start=begin,
                token_end=end,
            )
            if role.startswith("ARGM"):
                adjuncts.append(record)
            else:
                args.append(record)
        predicates.append(
            PredicateRecord(
                predicate_text=pred_surface or predicate_text,
                lemma=None,
                sense=None,
                sense_status="not_emitted",
                char_start=pred_start,
                char_end=pred_finish,
                token_start=pred_begin,
                token_end=pred_end,
                arguments=args,
                adjuncts=adjuncts,
                raw={"frame": frame},
            )
        )
    return SentenceParse(
        case_id=case_id,
        text=text,
        backend=str(raw_result.get("backend", "unknown")),
        model=str(raw_result.get("model", "unknown")),
        status="complete" if not errors or predicates else "unsupported",
        predicates=predicates,
        errors=errors,
        raw=raw_result.get("raw", raw_result),
    )


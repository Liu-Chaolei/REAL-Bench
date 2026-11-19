"""Played-audio reply-window and semantic judge helpers."""

from typing import Any

from tau2.utils.transcript.generated import sanitize_generated_text


def reply_text(summary: dict[str, Any], request_id: str) -> str:
    """Return configured functional reply evidence for a request window.

    Older evidence predates ``reply_evidence_mode`` and only contains ``text``.
    Falling back to that field keeps historical artifacts re-gradeable while all
    newly recorded default-mode evidence uses generated response text.
    """
    window = summary.get("reply_windows", {}).get(request_id, {})
    mode = summary.get("config", {}).get("reply_evidence_mode", "generated_text")
    v2 = summary.get("reply_evidence_version") == "persistent-reply-v2"
    if mode == "played_audio":
        return window.get("text", "")
    if mode == "both":
        text = window.get("generated_text")
        return sanitize_generated_text(text or ("" if v2 else window.get("text", "")))
    text = window.get("generated_text")
    return sanitize_generated_text(text or ("" if v2 else window.get("text", "")))

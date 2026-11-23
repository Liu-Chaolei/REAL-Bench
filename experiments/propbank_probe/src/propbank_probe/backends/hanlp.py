"""HanLP English SRL adapter.

HanLP versions expose slightly different English multi-task model constants and
document field names. This adapter resolves a documented constant dynamically
and keeps the raw document in the run artifact.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MODEL_NAMES = (
    "EN_TOK_LEM_POS_NER_SRL_UDEP_SDP_CON_MODERNBERT_BASE",
    "EN_TOK_LEM_POS_NER_SRL_DEP_SDP_CON_XLMR_BASE",
)


def _jsonable(value: Any) -> Any:
    """Convert HanLP document objects into JSON-compatible values."""

    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_json"):
        try:
            return _jsonable(value.to_json())
        except Exception:
            pass
    if hasattr(value, "items"):
        try:
            return {str(key): _jsonable(item) for key, item in value.items()}
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _field(document: Any, *names: str) -> Any:
    for name in names:
        try:
            if isinstance(document, dict) and name in document:
                return document[name]
            value = document.get(name) if hasattr(document, "get") else None
            if value is not None:
                return value
        except Exception:
            continue
    return None


def _token_offsets(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    """Align returned tokens to character offsets without retokenizing text."""

    offsets: list[tuple[int, int]] = []
    cursor = 0
    for token in tokens:
        token = str(token)
        position = text.find(token, cursor)
        if position < 0:
            # HanLP may normalize punctuation. Fall back to the next non-space
            # span so the record remains inspectable instead of dropping it.
            match = re.search(r"\S+", text[cursor:])
            if match is None:
                offsets.append((cursor, cursor))
                continue
            position = cursor + match.start()
            end = cursor + match.end()
        else:
            end = position + len(token)
        offsets.append((position, end))
        cursor = end
    return offsets


def _normalize_frames(srl: Any) -> list[list[tuple[str, str, int, int]]]:
    """Normalize one-sentence or one-document HanLP SRL nesting."""

    if srl is None:
        return []
    value = _jsonable(srl)
    if not isinstance(value, list) or not value:
        return []
    # A sentence is commonly represented as a list of predicate frames, while
    # a batch is represented as a list of sentence lists. A single frame is a
    # list whose first element is already a tuple.
    if isinstance(value[0], (list, tuple)) and value[0]:
        if isinstance(value[0][0], str) and len(value[0]) >= 4:
            value = [value]
        elif isinstance(value[0][0], list):
            value = value[0]
    frames: list[list[tuple[str, str, int, int]]] = []
    for frame in value:
        if not isinstance(frame, list):
            continue
        normalized = []
        for item in frame:
            if not isinstance(item, (list, tuple)) or len(item) < 4:
                continue
            span, role, begin, end = item[:4]
            try:
                normalized.append((str(span), str(role), int(begin), int(end)))
            except (TypeError, ValueError):
                continue
        if normalized:
            frames.append(normalized)
    return frames


class HanLPBackend:
    """Lazy HanLP English PropBank SRL backend."""

    name = "hanlp"

    def __init__(self, model_name: str | None = None):
        try:
            import hanlp
        except ImportError as exc:
            raise RuntimeError(
                "HanLP is not installed. Run `uv sync` in experiments/propbank_probe."
            ) from exc
        self._hanlp = hanlp
        self.model_name = model_name or self._discover_model_name()
        model_ref = self._resolve_model_ref(self.model_name)
        self._model = hanlp.load(model_ref)

    def _discover_model_name(self) -> str:
        mtl = getattr(self._hanlp.pretrained, "mtl", None)
        for name in DEFAULT_MODEL_NAMES:
            if mtl is not None and hasattr(mtl, name):
                return name
        raise RuntimeError(
            "No documented English HanLP SRL model constant was found. "
            "Pass --model with a HanLP model identifier."
        )

    def _resolve_model_ref(self, model_name: str):
        if "://" in model_name or model_name.endswith((".zip", ".json")):
            return model_name
        mtl = getattr(self._hanlp.pretrained, "mtl", None)
        if mtl is not None and hasattr(mtl, model_name):
            return getattr(mtl, model_name)
        return model_name

    def parse(self, text: str) -> dict[str, Any]:
        """Run SRL and return raw document data plus normalized frame tuples."""

        document = self._model(text)
        raw = _jsonable(document)
        tokens = _field(document, "tok/fine", "tok", "tokens")
        if not isinstance(tokens, list) or (tokens and isinstance(tokens[0], list)):
            tokens = tokens[0] if tokens else []
        tokens = [str(token) for token in (tokens or [])]
        frames = _normalize_frames(_field(document, "srl"))
        return {
            "backend": self.name,
            "model": self.model_name,
            "text": text,
            "tokens": tokens,
            "token_offsets": _token_offsets(text, tokens),
            "frames": frames,
            "raw": raw,
        }


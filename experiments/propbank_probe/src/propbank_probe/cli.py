"""Command-line runner for the isolated PropBank probe."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from propbank_probe.backends.hanlp import HanLPBackend
from propbank_probe.composition import analyze
from propbank_probe.mapping import load_mapping, map_sentence
from propbank_probe.models import CaseRecord, SentenceParse
from propbank_probe.normalize import normalize_raw
from propbank_probe.report import build_report, write_markdown


def _jsonl(path: Path):
    for line in path.read_text().splitlines():
        if line.strip():
            yield json.loads(line)


def _write_jsonl(path: Path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _versions() -> dict[str, str]:
    versions = {}
    for name in ("hanlp", "pydantic", "PyYAML"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def _backend(name: str, model: str | None):
    if name == "hanlp":
        return HanLPBackend(model)
    if name == "fixture":
        return FixtureBackend()
    raise ValueError(f"Unsupported backend: {name}")


class FixtureBackend:
    """Tiny deterministic backend used by tests and offline smoke runs."""

    name = "fixture"
    model_name = "fixture-v1"

    def parse(self, text: str):
        lower = text.lower()
        frames = []
        tokens = text.split()
        offsets = []
        cursor = 0
        for token in tokens:
            token_start = text.find(token, cursor)
            offsets.append((token_start, token_start + len(token)))
            cursor = token_start + len(token)

        def token_span(start: int, end: int):
            indexes = [
                index
                for index, (token_start, token_end) in enumerate(offsets)
                if token_end > start and token_start < end
            ]
            if not indexes:
                return 0, 0
            return indexes[0], indexes[-1] + 1

        def phrase_span(start: int, end: int, role: str):
            begin, finish = token_span(start, end)
            return text[start:end], role, begin, finish

        if "send" in lower:
            start = lower.index("send")
            end = start + 4
            frame = [phrase_span(start, end, "PRED")]
            remainder = text[end:]
            recipient_match = re.search(r"\bto\s+([^,.;!?]+)", remainder, re.IGNORECASE)
            content_end = recipient_match.start() if recipient_match else len(remainder)
            content = remainder[:content_end].strip()
            content = re.sub(r"^me\s+", "", content, flags=re.IGNORECASE)
            if content:
                content_start = end + remainder.find(content)
                frame.append(phrase_span(content_start, content_start + len(content), "ARG1"))
            if recipient_match:
                recipient = recipient_match.group(1).strip()
                recipient_start = end + recipient_match.start(1)
                frame.append(
                    phrase_span(recipient_start, recipient_start + len(recipient), "ARG2")
                )
            frames.append(frame)
        if "cancel" in lower:
            start = lower.index("cancel")
            frame = [phrase_span(start, start + 6, "PRED")]
            target_match = re.search(r"\bthe\s+[^.;!?]+", text[start + 6 :], re.IGNORECASE)
            if target_match:
                target_start = start + 6 + target_match.start()
                target = target_match.group(0).strip()
                frame.append(phrase_span(target_start, target_start + len(target), "ARG1"))
            frames.append(frame)
        if "give" in lower or "gave" in lower:
            surface = "gave" if "gave" in lower else "give"
            start = lower.index(surface)
            frame = [phrase_span(start, start + len(surface), "PRED")]
            for phrase, role in (("Lena", "ARG0"), ("the book", "ARG1"), ("Kai", "ARG2")):
                phrase_start = lower.find(phrase.lower())
                if phrase_start >= 0:
                    frame.append(phrase_span(phrase_start, phrase_start + len(phrase), role))
            frames.append(frame)
        return {
            "backend": self.name,
            "model": self.model_name,
            "text": text,
            "tokens": tokens,
            "token_offsets": offsets,
            "frames": frames,
            "raw": {"fixture": True},
        }


def parse_command(args):
    input_path = args.input.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    backend = _backend(args.backend, args.model)
    cases = [CaseRecord.model_validate(item) for item in _jsonl(input_path)]
    raw_records = []
    normalized = []
    errors = []
    for case in cases:
        try:
            raw = backend.parse(case.text)
            raw_records.append({"case_id": case.id, **raw})
            parse = normalize_raw(case.id, case.text, raw)
            # HanLP may expose only the surface form. Keep a conservative lemma
            # for mapping while marking the sense as unavailable.
            for predicate in parse.predicates:
                if predicate.lemma is None:
                    predicate.lemma = predicate.predicate_text.lower().strip(".,!?;:")
            normalized.append(parse.model_dump(mode="json"))
            if parse.errors:
                errors.extend({"case_id": case.id, **item} for item in parse.errors)
        except Exception as exc:
            errors.append({"case_id": case.id, "reason": type(exc).__name__, "detail": str(exc)})
            normalized.append(
                SentenceParse(
                    case_id=case.id,
                    text=case.text,
                    backend=getattr(backend, "name", args.backend),
                    model=str(getattr(backend, "model_name", "unknown")),
                    status="error",
                    errors=[{"reason": type(exc).__name__, "detail": str(exc)}],
                ).model_dump(mode="json")
            )
    _write_jsonl(output / "raw.jsonl", raw_records)
    _write_jsonl(output / "normalized.jsonl", normalized)
    (output / "errors.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in errors)
    )
    manifest = {
        "version": "propbank-probe-v1",
        "backend": args.backend,
        "model": getattr(backend, "model_name", "unknown"),
        "python": platform.python_version(),
        "package_versions": _versions(),
        "input": str(input_path),
        "input_sha256": _sha256(input_path),
        "command": " ".join(shlex.quote(value) for value in sys.argv),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(cases),
        "errors": len(errors),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(output / "normalized.jsonl")


def map_command(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = load_mapping(args.mapping_config.resolve())
    parses = [SentenceParse.model_validate(item) for item in _jsonl(args.input.resolve())]
    mappings = []
    compositions = []
    for parse in parses:
        mappings.extend(item.model_dump(mode="json") for item in map_sentence(parse, config))
        compositions.append(analyze(parse.case_id, parse.text).model_dump(mode="json"))
    _write_jsonl(output / "mapped.jsonl", mappings)
    _write_jsonl(output / "composition.jsonl", compositions)
    print(output / "mapped.jsonl")


def report_command(args):
    result = build_report(
        args.predictions.resolve(),
        args.mapped.resolve(),
        args.composition.resolve(),
        args.gold.resolve(),
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(result, output)
    output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    print(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse = subparsers.add_parser("parse", help="run an SRL backend")
    parse.add_argument("--input", type=Path, required=True)
    parse.add_argument("--backend", choices=["hanlp", "fixture"], default="hanlp")
    parse.add_argument("--model")
    parse.add_argument("--output", type=Path, required=True)
    parse.set_defaults(func=parse_command)

    mapping = subparsers.add_parser("map", help="map normalized predicates to candidate actions")
    mapping.add_argument("--input", type=Path, required=True)
    mapping.add_argument("--mapping-config", type=Path, required=True)
    mapping.add_argument("--output", type=Path, required=True)
    mapping.set_defaults(func=map_command)

    report = subparsers.add_parser("report", help="write the deterministic probe report")
    report.add_argument("--predictions", type=Path, required=True)
    report.add_argument("--mapped", type=Path, required=True)
    report.add_argument("--composition", type=Path, required=True)
    report.add_argument("--gold", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.set_defaults(func=report_command)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()


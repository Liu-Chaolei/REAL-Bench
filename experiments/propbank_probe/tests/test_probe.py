import json
from pathlib import Path

from propbank_probe.cli import FixtureBackend
from propbank_probe.composition import analyze
from propbank_probe.mapping import load_mapping, map_sentence
from propbank_probe.normalize import normalize_raw

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_normalization_preserves_predicates_and_spans():
    text = "Lena gave the book to Kai."
    raw = FixtureBackend().parse(text)
    parsed = normalize_raw("case", text, raw)

    assert parsed.status == "complete"
    assert len(parsed.predicates) == 1
    predicate = parsed.predicates[0]
    assert predicate.predicate_text == "gave"
    assert [argument.role for argument in predicate.arguments] == [
        "ARG0",
        "ARG1",
        "ARG2",
    ]
    assert [argument.text for argument in predicate.arguments] == [
        "Lena",
        "the book",
        "Kai.",
    ]
    assert parsed.raw["fixture"] is True


def test_mapping_keeps_ambiguous_send_candidates():
    text = "Send the tracking number to Wang."
    parsed = normalize_raw("case", text, FixtureBackend().parse(text))
    parsed.predicates[0].lemma = "send"
    config = load_mapping(ROOT / "data" / "gaia_mapping.yaml")
    mapped = map_sentence(parsed, config)

    assert mapped[0].status == "ambiguous"
    assert mapped[0].candidate_actions == ["send_message", "send_email"]
    assert {item.name for item in mapped[0].parameters} == {"content", "recipient"}


def test_composition_marks_condition_and_else_as_candidate():
    result = analyze(
        "conditional",
        "If the order has shipped, cancel it; otherwise send me the tracking number.",
    )
    assert result.status == "candidate"
    assert {item["relation"] for item in result.relations} == {
        "condition",
        "else_branch",
    }


def test_composition_rejects_orphan_else():
    result = analyze("orphan", "Otherwise send the number to Wang.")
    assert result.status == "unsupported"
    assert result.reason == "else_without_condition"


def test_cases_and_expected_are_jsonl():
    cases = [
        json.loads(line)
        for line in (ROOT / "data" / "cases.jsonl").read_text().splitlines()
        if line.strip()
    ]
    expected = [
        json.loads(line)
        for line in (ROOT / "data" / "expected.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(cases) == 40
    assert {item["id"] for item in cases} == {item["id"] for item in expected}


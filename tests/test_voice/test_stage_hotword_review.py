"""Regression coverage for literal sources and incremental phrase filtering."""

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def review(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("review_gaia2_stage_hotwords")


def term(text, source):
    return {"text": text, "source": source, "kind": "person", "reason": "test"}


def test_discarded_or_future_phrases_cannot_suppress_current_terms(review):
    evidence = {
        "stage1.tool_response.e": {"text": "Karlov náměstí", "stage": 1},
        # A discarded old address and a future phrase must not cover stage 1.
        "stage0.existing_catalog.0": {
            "text": "Karlov náměstí 12",
            "original_source": "old",
            "stage": 0,
        },
        "stage2.existing_catalog.0": {
            "text": "Karlov náměstí 13",
            "original_source": "future",
            "stage": 2,
        },
    }
    rows, removed = review.assemble(
        [
            {"stage": 0, "extracted": []},
            {
                "stage": 1,
                "extracted": [term("Karlov náměstí", "stage1.tool_response.e")],
            },
            {"stage": 2, "extracted": []},
        ],
        evidence,
    )
    assert rows[1]["active_vocabulary"] == {"Karlov náměstí": 50}
    assert rows[2]["active_vocabulary"] == rows[1]["active_vocabulary"]
    assert not removed[1]["terms"]


def test_kept_full_name_covers_alias_but_not_a_word_fragment(review):
    source = "stage1.tool_response.e"
    rows, removed = review.assemble(
        [
            {"stage": 0, "extracted": []},
            {
                "stage": 1,
                "extracted": [
                    term(t, source) for t in ["Akseli", "Akseli Korhonen", "Korh"]
                ],
            },
            {"stage": 2, "extracted": []},
        ],
        {source: {"stage": 1, "text": "Akseli Korhonen; Korh"}},
    )
    assert rows[1]["active_vocabulary"] == {"Akseli Korhonen": 50, "Korh": 50}
    assert removed[1]["terms"] == [{"text": "Akseli", "covered_by": "Akseli Korhonen"}]


def test_source_label_repair_does_not_allow_composed_or_future_names(review):
    payload = {
        "stages": [
            {
                "stage": 1,
                "sources": {
                    "stage1.existing_catalog.0": "Áine Ní Bhroin",
                    "stage1.tool_response.e": '{"first_name":"Áine","last_name":"Ní Bhroin"}',
                },
            }
        ]
    }
    raw = {"stages": [{"stage": 1, "terms": [term("Áine Ní Bhroin", "wrong")]}]}
    rows, repaired = review.grounded(json.dumps(raw), payload)
    assert rows[0]["extracted"][0]["source"] == "stage1.existing_catalog.0"
    assert len(repaired) == 1
    del payload["stages"][0]["sources"]["stage1.existing_catalog.0"]
    with pytest.raises(ValueError, match="No literal same-stage source"):
        review.grounded(json.dumps(raw), payload)


def test_binary_leaves_removed_but_text_preserved(review):
    omitted = []
    value = {"name": "Matěj Nováček", "attachment": "A" * 5000}
    clean = review.textual(value, omitted)
    assert clean["name"] == value["name"]
    assert clean["attachment"] == "[binary attachment omitted]"
    assert omitted[0]["chars"] == 5000


def test_weight_overflow_depends_on_cumulative_count_not_source(review):
    rows, _ = review.assemble(
        [
            {
                "stage": 0,
                "extracted": [
                    term(f"Name{i:03}", "stage0.tool_response.e") for i in range(49)
                ],
            },
            {"stage": 1, "extracted": [term("Name049", "stage1.tool_response.e")]},
            {
                "stage": 2,
                "extracted": [
                    term("Name050", "stage2.existing_catalog.0"),
                    term("Name000", "stage2.tool_response.e"),
                ],
            },
        ],
        {},
    )
    assert set(rows[0]["active_vocabulary"].values()) == {50}
    assert rows[1]["active_vocabulary"]["Name049"] == 50
    assert rows[2]["active_vocabulary"]["Name050"] == 5
    assert rows[2]["active_vocabulary"]["Name000"] == 50
    assert sum(w == 50 for w in rows[2]["active_vocabulary"].values()) == 50


def test_reviewed_loader_preserves_weights_and_stage_boundaries(tmp_path):
    from tau2.data_model.asr import HotwordSettings
    from tau2.data_model.tasks import Task
    from tau2.user.stage_asr_hotwords import _digest, load_stage_hotword_profile

    directory = ROOT / "data/gaia2_stage_hotwords/v2"
    ident = "0008_g6smim1r8ks1os676yok328htb91z4ws"
    path = directory / "tasks" / f"{ident}.json"
    if not path.exists():
        pytest.skip("Requires the offline generated v2 catalog")
    profile = json.loads(path.read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest.update(complete=True, profiles_sha256={ident: _digest(profile)})
    (tmp_path / "tasks").mkdir()
    local = tmp_path / "tasks" / path.name
    local.write_text(json.dumps(profile))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    task = Task.model_validate(
        next(
            t
            for t in json.loads(
                (ROOT / "data/tau2/domains/gaia2/tasks.json").read_text()
            )
            if t["id"] == ident
        )
    )
    settings = HotwordSettings(
        enabled=True,
        mode="gaia2_stage_gt",
        weight=50,
        max_terms=2000,
        catalog_dir=tmp_path,
    )
    loaded = load_stage_hotword_profile(task, settings)
    assert loaded.for_stage(0).vocabulary() == {}
    assert loaded.for_stage(1).vocabulary()["Sérgio Oliveira Silva"] == 50
    assert "Matěj Nováček" not in loaded.for_stage(1).vocabulary()
    assert loaded.for_stage(2).vocabulary()["Matěj Nováček"] == 50
    assert loaded.for_stage(2).vocabulary()["Akseli Korhonen"] == 50
    previous = {}
    for stage in range(3):
        current = loaded.for_stage(stage).vocabulary()
        assert previous.items() <= current.items()
        previous = current
    # Even a freshly sealed profile cannot cite future-stage response evidence.
    source = next(
        k for k in profile["sources"] if k.startswith("stage2.tool_response.")
    )
    profile["sources"][source]["stage"] = 0
    manifest["profiles_sha256"][ident] = _digest(profile)
    local.write_text(json.dumps(profile))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="same-stage evidence"):
        load_stage_hotword_profile(task, settings)


def test_active_realtime_configuration_loads_all_reviewed_catalogs():
    from tau2.config import DEFAULT_GAIA2_STAGE_HOTWORDS_DIRECTORY
    from tau2.data_model.tasks import Task
    from tau2.user.stage_asr_hotwords import load_stage_hotword_profile
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    config = QwenAgentConfig.model_validate_json(
        (
            ROOT
            / "integrations/qwen_audio_agent/config.gaia2.realtime.dynamic.metrics.deepseek-user.json"
        ).read_text()
    )
    assert DEFAULT_GAIA2_STAGE_HOTWORDS_DIRECTORY == "gaia2_stage_hotwords/v2"
    assert config.asr_hotwords.max_terms == 2000
    tasks = json.loads((ROOT / "data/tau2/domains/gaia2/tasks.json").read_text())
    assert len(tasks) == 1100
    for raw in tasks:
        profile = load_stage_hotword_profile(
            Task.model_validate(raw), config.asr_hotwords
        )
        previous = {}
        for stage in range(3):
            vocabulary = profile.for_stage(stage).vocabulary()
            assert previous.keys() <= vocabulary.keys()
            assert all(vocabulary[word] >= weight for word, weight in previous.items())
            assert len(vocabulary) <= 2000
            assert sum(w == 50 for w in vocabulary.values()) <= 50
            assert set(vocabulary.values()) <= {5, 50}
            assert sum(w == 50 for w in vocabulary.values()) == min(50, len(vocabulary))
            assert list(vocabulary.values()) == [50] * min(50, len(vocabulary)) + [
                5
            ] * max(0, len(vocabulary) - 50)
            previous = vocabulary

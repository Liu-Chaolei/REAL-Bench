"""Fixed stage vocabularies are grounded, progressive and evaluator-private."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau2.config import DEFAULT_QWEN_ASR_STREAMING_MODEL
from tau2.data_model.asr import HotwordEntry, HotwordSettings
from tau2.data_model.message import UserMessage
from tau2.data_model.tasks import Task
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.user.stage_asr_hotwords import load_stage_hotword_profile
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "data/gaia2_stage_hotwords/v1"


@pytest.fixture(scope="module")
def gaia_tasks():
    return json.loads((ROOT / "data/tau2/domains/gaia2/tasks.json").read_text())


@pytest.fixture
def gaia_task(gaia_tasks):
    return Task.model_validate(
        next(
            t for t in gaia_tasks if t["id"] == "0007_tb52xggog13zgslxysl2jpf7k9h3wgz0"
        )
    )


def settings(**updates):
    updates.setdefault("catalog_dir", CATALOG)
    return HotwordSettings(
        enabled=True, mode="gaia2_stage_gt", weight=50, max_terms=50, **updates
    )


def test_every_fixed_task_catalog_matches_the_current_runtime_task(gaia_tasks):
    assert len(gaia_tasks) == 1100
    for raw in gaia_tasks:
        profile = load_stage_hotword_profile(Task.model_validate(raw), settings())
        previous = {}
        for stage in range(3):
            vocabulary = profile.for_stage(stage).vocabulary()
            assert previous.items() <= vocabulary.items()
            assert len(vocabulary) <= 50
            assert all(weight == 50 for weight in vocabulary.values())
            previous = vocabulary


def test_pilot_names_appear_only_in_their_released_stage(gaia_task):
    profile = load_stage_hotword_profile(gaia_task, settings())
    assert profile.vocabulary() == {"Lysverket": 50}
    assert profile.for_stage(1).vocabulary() == {"Lysverket": 50, "Kaida Rao": 50}
    assert profile.for_stage(2).vocabulary() == {
        "Lysverket": 50,
        "Kaida Rao": 50,
        "Åse": 50,
    }
    assert "stage2" not in " ".join(profile.for_stage(0).entries[0].sources)
    with pytest.raises(ValueError, match="No ASR vocabulary"):
        profile.for_stage(3)


def test_changed_source_or_forged_activation_is_rejected(gaia_task, tmp_path):
    changed = gaia_task.model_copy(deep=True)
    changed.scenario.stages[0].request += " Also buy a car."
    with pytest.raises(ValueError, match="does not match"):
        load_stage_hotword_profile(changed, settings())
    (tmp_path / "tasks").mkdir()
    shutil.copyfile(CATALOG / "manifest.json", tmp_path / "manifest.json")
    path = tmp_path / "tasks" / f"{gaia_task.id}.json"
    data = json.loads((CATALOG / "tasks" / path.name).read_text())
    data["stages"][0]["active_vocabulary"]["Kaida Rao"] = 50
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="cumulative"):
        load_stage_hotword_profile(gaia_task, settings(catalog_dir=tmp_path))


def test_weight_validation_and_streaming_only_configuration():
    HotwordEntry(text="Lysverket", weight=50, sources=("stage0.source_task",))
    with pytest.raises(ValueError):
        HotwordSettings(enabled=True, weight=30)
    with pytest.raises(ValueError, match="max_terms"):
        HotwordSettings(enabled=True, weight=50)
    with pytest.raises(ValueError, match="streaming"):
        QwenAgentConfig(
            asr_provider="qwen",
            asr_model="qwen-audio-3.1-asr-flash",
            asr_base_url="https://dashscope.invalid",
            asr_hotwords=settings(),
        )
    QwenAgentConfig(
        asr_provider="qwen",
        asr_model=DEFAULT_QWEN_ASR_STREAMING_MODEL,
        asr_base_url="https://dashscope.invalid",
        asr_hotwords=settings(),
    )


def test_stage_activation_waits_for_actual_request_speech():
    stages = []
    orchestrator = object.__new__(FullDuplexOrchestrator)
    orchestrator.agent = SimpleNamespace(set_observer_stage=stages.append)
    orchestrator.user = SimpleNamespace(scenario_dynamic_stage_ids={"next-task": 1})
    orchestrator._activate_observer_stage(
        UserMessage(
            role="user",
            contains_speech=False,
            raw_data={"scenario_input_id": "next-task"},
        )
    )
    assert stages == []
    orchestrator._activate_observer_stage(
        UserMessage(role="user", contains_speech=True)
    )
    assert stages == [1]
    orchestrator._activate_observer_stage(
        UserMessage(role="user", contains_speech=True)
    )
    assert stages == [1]


def test_voice_builder_loads_catalog_without_querying_environment_or_llm(
    monkeypatch, tmp_path, gaia_task
):
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.domains.mock.environment import get_environment
    from tau2.runner import build

    environment = get_environment()

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Fixed hotwords must not read environment material or call an LLM"
        )

    monkeypatch.setattr(environment, "get_user_asr_materials", forbidden)
    monkeypatch.setattr("tau2.user.asr_hotwords.build_hotword_profile", forbidden)
    monkeypatch.setattr(build, "build_environment", lambda *a, **k: environment)
    monkeypatch.setattr(build, "_build_env_kwargs", lambda *a, **k: {})
    monkeypatch.setattr(build, "build_voice_user", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        build, "FullDuplexOrchestrator", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    config = VoiceRunConfig(
        domain="gaia2",
        language="en",
        llm_user="not-called",
        audio_native_config=AudioNativeConfig(
            provider="qwen_agent",
            qwen_agent_config=QwenAgentConfig(
                asr_provider="qwen",
                asr_model=DEFAULT_QWEN_ASR_STREAMING_MODEL,
                asr_base_url="https://dashscope.invalid",
                asr_hotwords=settings(),
            ),
        ),
    )
    result = build.build_voice_orchestrator(config, gaia_task, audio_taps_dir=tmp_path)
    try:
        profile = result.agent.observer_hotwords
        assert profile.for_stage(0).vocabulary() == {"Lysverket": 50}
        adapter = result.agent.adapter
        assert adapter.transcriber.hotwords is profile
        assert "Lysverket" not in adapter.provider.config.model_dump_json()
        assert "Lysverket" not in result.agent.domain_policy
        assert (tmp_path / "observer_hotwords.json").is_file()
    finally:
        environment.close()

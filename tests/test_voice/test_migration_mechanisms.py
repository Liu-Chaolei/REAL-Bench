"""Focused checks for the backup feature migration mechanisms."""

import json
from pathlib import Path
from types import SimpleNamespace

from tau2.config import DEFAULT_QWEN_ASR_STREAMING_MODEL
from tau2.data_model.asr import HotwordSettings
from tau2.data_model.audio import AudioEncoding
from tau2.data_model.message import UserMessage
from tau2.data_model.tasks import Task
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator
from tau2.user.stage_asr_hotwords import load_stage_hotword_profile
from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
from tau2.voice.audio_native.qwen_agent.discrete_time_adapter import (
    DiscreteTimeQwenAgentAdapter,
)

ROOT = Path(__file__).resolve().parents[2]


def test_current_stage_catalog_has_only_current_tasks_and_loads_a_profile():
    manifest = json.loads(
        (ROOT / "data/gaia2_stage_hotwords/v2/manifest.json").read_text()
    )
    assert manifest["version"] == "gaia2-stage-hotwords-v2"
    assert manifest["tasks"] == 1000
    assert manifest["async_tasks"] == 0
    profile_files = list(
        (ROOT / "data/gaia2_stage_hotwords/v2/tasks").glob("*.json")
    )
    assert len(profile_files) == 1000
    assert not any("__async_" in path.name for path in profile_files)

    task = Task.model_validate(
        json.loads((ROOT / "data/tau2/domains/gaia2/tasks.json").read_text())[0]
    )
    profile = load_stage_hotword_profile(
        task,
        HotwordSettings(
            enabled=True,
            mode="gaia2_stage_gt",
            weight=50,
            max_terms=50,
            catalog_dir=ROOT / "data/gaia2_stage_hotwords/v2",
        ),
    )
    assert profile.task_id == task.id
    assert all(
        entry.weight in {5, 50}
        for stage in profile.stages
        for entry in stage.entries
    )


def test_stage_activation_waits_for_actual_speech():
    stages = []
    orchestrator = object.__new__(FullDuplexOrchestrator)
    orchestrator.agent = SimpleNamespace(set_observer_stage=stages.append)
    orchestrator.user = SimpleNamespace(scenario_dynamic_stage_ids={"next": 1})
    orchestrator.scenario_stages = None

    orchestrator._activate_observer_stage(
        UserMessage(
            role="user",
            contains_speech=False,
            raw_data={"scenario_input_id": "next"},
        )
    )
    orchestrator._activate_observer_stage(
        UserMessage(
            role="user",
            contains_speech=True,
            raw_data={"scenario_input_id": "next"},
        )
    )
    assert stages == [1]


def test_streaming_qwen_uses_native_pcm_playback_format():
    config = QwenAgentConfig(
        asr_provider="qwen",
        asr_model=DEFAULT_QWEN_ASR_STREAMING_MODEL,
        asr_base_url="https://dashscope.aliyuncs.com",
    )
    adapter = DiscreteTimeQwenAgentAdapter(
        200,
        config,
        provider=SimpleNamespace(),
        transcriber=SimpleNamespace(),
    )
    assert adapter.playback_audio_format.encoding == AudioEncoding.PCM_S16LE
    assert adapter.playback_audio_format.sample_rate == 24000
    assert adapter.playback_bytes_per_tick == 9600
    assert adapter.playback.format == adapter.playback_audio_format

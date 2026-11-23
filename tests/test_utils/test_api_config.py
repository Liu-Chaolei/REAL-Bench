"""Offline routing contracts for the shared private API configuration."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau2.utils.api_config import ApiConfiguration, get_api_configuration
from tau2.utils.env_config import read_config_environment


@pytest.fixture
def api_dir(tmp_path):
    (tmp_path / "config.env").write_text("""A_MODEL=alpha
A_BASE_URL=https://alpha.invalid/v1
A_API_KEY=secret-alpha
B_MODEL=beta
B_BASE_URL=https://beta.invalid/v1
B_API_KEY=secret-beta
AUDIO_MODEL=qwen-audio-3.1-realtime-flash
""")
    (tmp_path / "qw.env").write_text("""GAIA2_BACKEND_MODEL=${B_MODEL}
GAIA2_BACKEND_API_BASE=${B_BASE_URL}
GAIA2_BACKEND_API_KEY=${B_API_KEY}
GAIA2_BACKEND_REASONING_EFFORT=low
QWEN_AUDIO_REALTIME_PROVIDER=dashscope
QWEN_AUDIO_REALTIME_MODEL=${AUDIO_MODEL}
QWEN_AUDIO_REALTIME_BASE_URL=wss://audio.invalid/realtime
DASHSCOPE_API_KEY=${B_API_KEY}
OPENCODE_CONFIG_CONTENT='{"options":{"apiKey":"${GAIA2_BACKEND_API_KEY}"}}'
""")
    text = ""
    for role, supplier, protocol in (
        ("USER", "A", "chat"),
        ("JUDGE", "B", "responses"),
        ("AUTHOR", "A", "chat"),
        ("REVIEWER", "B", "responses"),
    ):
        for suffix in ("MODEL", "BASE_URL", "API_KEY"):
            text += f"REALBENCH_{role}_{suffix}=${{{supplier}_{suffix}}}\n"
        text += f"REALBENCH_{role}_API={protocol}\n"
    text += """REALBENCH_USER_DECISION_TEMPERATURE=0.2
REALBENCH_ASR_PROVIDER=qwen
REALBENCH_ASR_MODEL=qwen-audio-3.1-asr-flash
REALBENCH_ASR_BASE_URL=https://asr.invalid
REALBENCH_ASR_API_KEY=${A_API_KEY}
REALBENCH_TTS_PROVIDER=qwen
REALBENCH_TTS_MODEL=qwen-audio-3.1-tts-flash
REALBENCH_TTS_BASE_URL=https://tts.invalid
REALBENCH_TTS_API_KEY=${A_API_KEY}
REALBENCH_TTS_VOICE=loongbella_v3.1
REALBENCH_TTS_VOICES_JSON='{"p":"Brian_v3.1"}'
"""
    (tmp_path / "realbench.env").write_text(text)
    return str(tmp_path)


def test_files_are_authoritative_and_snapshot_is_stable(api_dir, monkeypatch):
    monkeypatch.setenv("A_API_KEY", "wrong-ambient-secret")
    api = get_api_configuration(api_dir)
    assert api.text("USER").arguments()["api_key"] == "secret-alpha"
    assert api.text("USER_DECISION").args == {"temperature": 0.2}
    assert api.text("USER").args == {}
    assert api.text("EXECUTION").litellm_model == "openai/responses/beta"
    pool = Path(api_dir) / "config.env"
    pool.write_text(pool.read_text().replace("alpha", "changed"))
    assert get_api_configuration(api_dir).text("USER").model == "alpha"
    assert (
        read_config_environment(str(Path(api_dir) / "realbench.env"))["A_MODEL"]
        == "alpha"
    )


@pytest.mark.parametrize(
    "extra,match",
    [
        ("REALBENCH_EXECUTION_MODEL=${A_MODEL}\n", "Incomplete"),
        ("CYCLE=${CYCLE}\nREALBENCH_USER_TEMPERATURE=${CYCLE}\n", "Cyclic"),
        ("REALBENCH_USER_TEMPERATURE=${MISSING}\n", "Missing"),
        ("A_MODEL=duplicate\n", "Duplicate"),
    ],
)
def test_invalid_configuration_fails_without_credentials(api_dir, extra, match):
    path = Path(api_dir) / "realbench.env"
    path.write_text(path.read_text() + extra)
    with pytest.raises(ValueError, match=match) as error:
        ApiConfiguration(api_dir).text("EXECUTION" if "EXECUTION" in extra else "USER")
    assert "secret-alpha" not in str(error.value)


def test_qwen_config_routes_agent_caller_and_audio_independently(api_dir):
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig
    from tau2.voice.qwen_tts import _resolve_key

    config = QwenAgentConfig.model_validate({"api_config_dir": api_dir})
    env = config.resolved_environment({"OPENAI_API_KEY": "wrong"})
    assert env["OPENAI_MODEL"] == "beta"
    assert env["OPENAI_API_KEY"] == "secret-beta"
    assert "REALBENCH_JUDGE_API_KEY" not in env
    assert config.user_llm_settings()[0] == "openai/alpha"
    assert config.asr_base_url == "https://asr.invalid"
    assert config.tts.base_url == "https://tts.invalid"
    assert _resolve_key(config.tts) == "secret-alpha"
    assert config.tts.voices["p"] == "Brian_v3.1"
    dumped = config.model_dump_json()
    assert "secret-alpha" not in dumped and "secret-beta" not in dumped
    assert QwenAgentConfig.model_validate_json(dumped).asr_model == config.asr_model


def test_diagnostics_and_authoring_use_their_own_connections(
    api_dir, tmp_path, monkeypatch
):
    from tau2.data_model.semantic_communication import SemanticCommunicateSettings
    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.model import RequestBudget, TauJsonModel
    from tau2.task_authoring.gaia2_series.generation import (
        fallback_profile,
        resolve_profile,
    )
    from tau2.task_authoring.gaia2_series.schema import AuthoringConfig

    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content='{"ok":true}', usage=None, cost=None)

    monkeypatch.setattr("tau2.utils.llm_utils.generate", generate)
    metrics = MetricsConfig.model_validate({"api_config_dir": api_dir})
    TauJsonModel(
        metrics, "execution", tmp_path / "requests", RequestBudget(2)
    ).complete_sync("test", {})
    assert calls[0]["model"] == "openai/responses/beta"
    assert calls[0]["api_base"] == "https://beta.invalid/v1"
    assert calls[0]["api_key"] == "secret-beta"
    authoring = AuthoringConfig.model_validate({"api_config_dir": api_dir})
    model, args, _ = resolve_profile(authoring, authoring.author)
    assert (model, args["api_key"]) == ("openai/alpha", "secret-alpha")
    assert fallback_profile(authoring, authoring.author) is None
    judge = SemanticCommunicateSettings.model_validate(
        {"api_role": "REPLY_JUDGE", "api_config_dir": api_dir}
    )
    assert judge.arguments()["api_key"] == "secret-beta"
    for value in (metrics, authoring, judge):
        assert "secret-" not in value.model_dump_json()
    assert all(
        "secret-" not in p.read_text() for p in (tmp_path / "requests").glob("*.json")
    )


def test_cache_identity_changes_for_model_but_not_key(api_dir):
    from tau2.diagnostics.config import MetricsConfig

    first = MetricsConfig.model_validate({"api_config_dir": api_dir}).fingerprint()
    # A fresh directory stands for a new process with a new startup snapshot.
    import shutil

    other = Path(api_dir) / "second"
    other.mkdir()
    for name in ("config.env", "qw.env", "realbench.env"):
        shutil.copyfile(Path(api_dir) / name, other / name)
    pool = other / "config.env"
    pool.write_text(pool.read_text().replace("secret-beta", "rotated-key"))
    # Ignore config paths, compare the resolved public connection identity.
    a = ApiConfiguration(api_dir).text("JUDGE").public()
    b = ApiConfiguration(other).text("JUDGE").public()
    a.pop("source")
    b.pop("source")
    assert a == b
    assert first
    pool.write_text(pool.read_text().replace("B_MODEL=beta", "B_MODEL=other"))
    assert ApiConfiguration(other).text("JUDGE").model == "other"


def test_json_references_escape_credentials_correctly(api_dir):
    pool = Path(api_dir) / "config.env"
    pool.write_text(
        pool.read_text().replace(
            "B_API_KEY=secret-beta", "B_API_KEY='key-with-\"quotes'"
        )
    )
    api = ApiConfiguration(api_dir, "qw")
    value = api.json_value("OPENCODE_CONFIG_CONTENT")
    assert json.loads(json.dumps(value))["options"]["apiKey"] == 'key-with-"quotes'


def test_saved_configuration_is_readable_without_private_files(api_dir):
    from tau2.data_model.semantic_communication import SemanticCommunicateSettings
    from tau2.diagnostics.config import MetricsConfig
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    values = [
        (QwenAgentConfig, {"api_config_dir": api_dir}),
        (MetricsConfig, {"api_config_dir": api_dir}),
        (
            SemanticCommunicateSettings,
            {"api_config_dir": api_dir, "api_role": "REPLY_JUDGE"},
        ),
    ]
    snapshots = [
        (cls, cls.model_validate(value).model_dump_json()) for cls, value in values
    ]
    import shutil

    from tau2.utils.api_config import _configuration_snapshot

    shutil.rmtree(api_dir)
    _configuration_snapshot.cache_clear()
    for cls, snapshot in snapshots:
        assert cls.model_validate_json(snapshot)


def test_judge_from_user_affects_only_judges(api_dir):
    from tau2.diagnostics.config import MetricsConfig
    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    metrics = MetricsConfig.model_validate(
        {"api_config_dir": api_dir, "api_judge_role": "USER"}
    )
    assert metrics.profile("execution").model == "openai/alpha"
    config = QwenAgentConfig.model_validate({"api_config_dir": api_dir})
    assert config.resolved_environment({})["OPENAI_MODEL"] == "beta"


def test_active_generator_overrides_directory_before_loading(api_dir, tmp_path):
    import yaml

    from tau2.metrics.active.config import ActiveMetricsConfig

    config = yaml.safe_load(
        Path("examples/active-metrics.persistent-task.yaml").read_text()
    )
    path = tmp_path / "active.yaml"
    path.write_text(yaml.safe_dump(config))
    loaded = ActiveMetricsConfig.load(path, api_dir)
    profile = loaded.persistent_task.generator.model
    assert profile.model == "openai/alpha"
    assert profile.arguments()["api_key"] == "secret-alpha"


def test_smoke_launch_keeps_credentials_off_argv(api_dir, tmp_path, monkeypatch):
    import importlib.util

    from tau2.voice.audio_native.qwen_agent.config import QwenAgentConfig

    spec = importlib.util.spec_from_file_location(
        "api_smoke_test", "integrations/qwen_audio_agent/smoke.py"
    )
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    captured = []

    class Process:
        stdout = []

        def __init__(self, command, **kwargs):
            captured.append(command)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self):
            return 0

    monkeypatch.setattr(smoke.subprocess, "Popen", Process)
    monkeypatch.setattr("tau2.utils.utils.DATA_DIR", tmp_path)
    args = SimpleNamespace(
        save_to="test",
        dry_run=True,
        resume=False,
        judge_from_user_llm=True,
        domain="gaia2",
        seed=300,
        max_concurrency=1,
        max_seconds=100,
        realtime_generation=False,
        metrics_config=None,
        active_metrics_config=None,
        persistent_task_config=None,
        user_audio_renderer="standard",
        saasr_config=None,
        gaia2_allow_stale_runtime=False,
        task_ids=None,
        num_tasks=1,
    )
    config = QwenAgentConfig.model_validate({"api_config_dir": api_dir})
    with pytest.raises(SystemExit) as error:
        smoke.run_shared(args, config)
    assert error.value.code == 0
    assert "secret-" not in json.dumps(captured)
    assert "--judge-from-user-llm" in captured[0]
    assert config.resolved_environment({})["OPENAI_MODEL"] == "beta"


def test_role_timeout_reaches_diagnostic_request(api_dir, tmp_path, monkeypatch):
    from tau2.diagnostics.config import MetricsConfig
    from tau2.diagnostics.model import RequestBudget, TauJsonModel

    path = Path(api_dir) / "realbench.env"
    path.write_text(path.read_text() + "REALBENCH_EXECUTION_TIMEOUT_SECONDS=17\n")
    captured = []

    def generate(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(content='{"ok": true}', usage=None, cost=None)

    monkeypatch.setattr("tau2.utils.llm_utils.generate", generate)
    config = MetricsConfig.model_validate({"api_config_dir": api_dir})
    TauJsonModel(
        config, "execution", tmp_path / "cache", RequestBudget(1)
    ).complete_sync("test", {})
    assert captured[0]["timeout"] == 17

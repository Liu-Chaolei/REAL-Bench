"""Automatic per-task hotwords are sourced, reproducible and private to listening."""

import asyncio
import base64
import io
import json
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tau2.data_model.asr import (
    HotwordCandidate,
    HotwordEntry,
    HotwordMaterials,
    HotwordProfile,
    HotwordSettings,
)
from tau2.data_model.message import AssistantMessage
from tau2.data_model.tasks import (
    EvaluationCriteria,
    StructuredUserInstructions,
    Task,
    UserScenario,
)
from tau2.user import asr_hotwords
from tau2.voice.audio_native.qwen_agent.config import (
    QwenAgentConfig,
    SpeechToSpeechConfig,
)
from tau2.voice.audio_native.qwen_agent.qwen_transcription import (
    QwenPlayedAudioTranscriber,
)


def task():
    return Task(
        id="any-task-id",
        user_scenario=UserScenario(
            instructions=StructuredUserInstructions(
                domain="mock",
                reason_for_call="Ask Alice Johnson about Northwind.",
                known_info="I am Åse Kjellstad.",
                unknown_info="SECRET_UNKNOWN",
                task_instructions="GENERIC_BEHAVIOR_ONLY",
            )
        ),
        evaluation_criteria=EvaluationCriteria(communicate_info=["SECRET_ANSWER"]),
    )


def reply(terms):
    return AssistantMessage(
        role="assistant",
        content=json.dumps({"terms": terms}),
        usage={"prompt_tokens": 10},
        cost=0.01,
        generation_time_seconds=0.1,
    )


def test_literals_provenance_unicode_priority_and_cache(monkeypatch, tmp_path):
    calls = []

    def extract(**kwargs):
        calls.append(kwargs)
        text = kwargs["messages"][-1].content
        assert "SECRET_ANSWER" not in text and "SECRET_UNKNOWN" not in text
        assert "GENERIC_BEHAVIOR_ONLY" not in text
        return reply(
            [
                {"text": "Alice Johnson", "source": "task.reason_for_call"},
                {"text": "Åse Kjellstad", "source": "task.known_info"},
                {"text": "Fabricated Person", "source": "task.reason_for_call"},
                {"text": "Northwind", "source": "reference.action"},
            ]
        )

    monkeypatch.setattr(asr_hotwords, "generate", extract)
    material = HotwordMaterials(
        candidates=(
            HotwordCandidate(
                text="Alice  Johnson", source="Contacts.a.name", priority=1
            ),
            HotwordCandidate(text="Cafe\u0301", source="Contacts.b.company"),
            HotwordCandidate(text="999999", source="price"),
            HotwordCandidate(text="Zeta", source="catalog"),
        )
    )
    settings = HotwordSettings(enabled=True, cache_dir=tmp_path, max_terms=3)
    first = asr_hotwords.build_hotword_profile(
        task(),
        material,
        settings,
        language="en",
        model="test",
        model_args={"api_key": "private-key"},
    )
    assert first.vocabulary() == {"Alice Johnson": 4, "Åse Kjellstad": 4, "Café": 4}
    assert first.entries[0].sources == ("Contacts.a.name", "task.reason_for_call")
    assert first.rejected_terms == 3 and first.truncated_terms == 1
    second = asr_hotwords.build_hotword_profile(
        task(),
        material,
        settings,
        language="en",
        model="test",
        model_args={"api_key": "private-key"},
    )
    assert second.cache_hit and len(calls) == 1
    assert first.vocabulary_sha256 == second.vocabulary_sha256
    assert "private-key" not in next(tmp_path.glob("*.json")).read_text()
    mutated = second.vocabulary()
    mutated["injected"] = 5
    assert "injected" not in second.vocabulary()
    changed = asr_hotwords.build_hotword_profile(
        task(), material, settings, language="en", model="other-model", model_args={}
    )
    assert changed.cache_key != first.cache_key and len(calls) == 2


def test_concurrent_trials_share_one_extraction(monkeypatch, tmp_path):
    calls = []

    def extract(**kwargs):
        calls.append(1)
        return reply([])

    monkeypatch.setattr(asr_hotwords, "generate", extract)

    def run(_):
        return asr_hotwords.build_hotword_profile(
            task(),
            HotwordMaterials(),
            HotwordSettings(enabled=True, cache_dir=tmp_path),
            language="en",
            model="test",
            model_args={},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        profiles = list(pool.map(run, range(4)))
    assert len(calls) == 1
    assert len({p.cache_key for p in profiles}) == 1
    assert sum(p.cache_hit for p in profiles) == 3


def test_future_events_and_reference_answers_cannot_change_the_vocabulary(
    monkeypatch, tmp_path
):
    from tau2.data_model.scenario import ScenarioAction, ScenarioSpec

    seen = []

    def extract(**kwargs):
        seen.append(kwargs["messages"][-1].content)
        return reply([])

    monkeypatch.setattr(asr_hotwords, "generate", extract)
    current = task()
    current.scenario = ScenarioSpec(
        start_time=0,
        source_id="test",
        source_sha256="test",
        snapshot="a" * 64,
        category="test",
        events=[
            ScenarioAction(
                id="later",
                kind="user",
                tool="send_message",
                arguments={"content": "SECRET_FUTURE"},
            )
        ],
    )
    settings = HotwordSettings(enabled=True, cache_dir=tmp_path)
    first = asr_hotwords.build_hotword_profile(
        current,
        HotwordMaterials(),
        settings,
        language="en",
        model="test",
        model_args={},
    )
    current.scenario.events[0].arguments["content"] = "OTHER_FUTURE"
    current.evaluation_criteria.communicate_info = ["OTHER_ANSWER"]
    second = asr_hotwords.build_hotword_profile(
        current,
        HotwordMaterials(),
        settings,
        language="en",
        model="test",
        model_args={},
    )
    assert len(seen) == 1 and "SECRET_FUTURE" not in seen[0]
    assert second.cache_hit and second.cache_key == first.cache_key


def test_empty_material_and_generation_failure_are_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(asr_hotwords, "generate", lambda **kwargs: reply([]))
    profile = asr_hotwords.build_hotword_profile(
        task(),
        HotwordMaterials(),
        HotwordSettings(cache_dir=tmp_path),
        language="en",
        model="empty",
        model_args={},
    )
    assert profile.vocabulary() == {}
    monkeypatch.setattr(
        asr_hotwords, "generate", lambda **kwargs: reply([{"bad": "schema"}])
    )
    with pytest.raises(ValueError):
        asr_hotwords.build_hotword_profile(
            task(),
            HotwordMaterials(),
            HotwordSettings(cache_dir=tmp_path),
            language="en",
            model="malformed",
            model_args={},
        )
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_gaia2_material_is_initial_personal_entities():
    from tau2.domains.gaia2.asr_materials import user_asr_materials

    materials = user_asr_materials(
        [
            {
                "name": "Contacts",
                "app_state": {
                    "contacts": {
                        "id": {
                            "first_name": "Kåre",
                            "last_name": "Østensen",
                            "city_living": "Oslo",
                            "description": "private reference-like prose",
                            "email": "private@example.com",
                        }
                    }
                },
            },
            {
                "name": "InternalContacts",
                "app_state": {
                    "contacts": {
                        "hidden": {"first_name": "Invisible", "last_name": "Person"}
                    }
                },
            },
            {"name": "Messages", "app_state": {"name_to_id": {"Alice Johnson": "x"}}},
            {
                "name": "AgentUserInterface",
                "app_state": {"messages": ["SECRET_ANSWER"]},
            },
        ]
    )
    words = [c.text for c in materials.candidates]
    assert "Kåre Østensen" in words and "Alice Johnson" in words
    assert "Invisible Person" not in words
    assert "SECRET_ANSWER" not in str(materials) and "private@example.com" not in str(
        materials
    )


def test_retail_only_resolves_the_named_callers_background():
    from tau2.domains.retail.environment import get_environment, get_tasks

    env = get_environment()
    try:
        current = get_tasks()[0]
        materials = env.get_user_asr_materials(current)
        assert materials.candidates
        assert any(c.text == "Yusuf Rossi" for c in materials.candidates)
        assert not any("@" in c.text for c in materials.candidates)
        unrelated = current.model_copy(deep=True)
        unrelated.user_scenario.instructions.known_info = "Unknown caller"
        assert env.get_user_asr_materials(unrelated).candidates == ()
    finally:
        env.close()


def test_shared_voice_builder_binds_words_to_only_this_calls_observer(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.domains.retail.environment import get_tasks
    from tau2.runner import build

    seen = []

    def extract(**kwargs):
        seen.append(kwargs)
        return reply([])

    monkeypatch.setattr(asr_hotwords, "generate", extract)
    monkeypatch.setattr(
        build, "build_voice_user", lambda *args, **kwargs: SimpleNamespace()
    )
    monkeypatch.setattr(
        build, "FullDuplexOrchestrator", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    system = qwen_config(
        speech_to_speech=SpeechToSpeechConfig(),
        asr_hotwords=HotwordSettings(enabled=True, cache_dir=tmp_path / "cache"),
    )
    config = VoiceRunConfig(
        domain="retail",
        language="en",
        audio_native_config=AudioNativeConfig(
            provider="qwen_agent", qwen_agent_config=system
        ),
        llm_user="offline-extractor",
    )
    orchestrator = build.build_voice_orchestrator(
        config,
        get_tasks()[0],
        audio_taps_dir=tmp_path / "taps",
    )
    try:
        assert len(seen) == 1 and seen[0]["model"] == "offline-extractor"
        words = orchestrator.agent.observer_hotwords
        assert "Yusuf Rossi" in words.vocabulary()
        adapter = orchestrator.agent.adapter
        assert adapter.transcriber.hotwords is words
        assert adapter.config.speech_to_speech.asr_model == "mimo-v2.5-asr"
        assert "Yusuf Rossi" not in orchestrator.agent.domain_policy
        assert not hasattr(adapter.provider.config, "observer_hotwords")
        assert (tmp_path / "taps/observer_hotwords.json").is_file()
        assert system.asr_hotwords.enabled
    finally:
        orchestrator.environment.close()


def profile(name="Kåre Østensen"):
    return HotwordProfile(
        entries=(HotwordEntry(text=name, weight=4, sources=("Contacts.test",)),),
        cache_key="test",
        materials_sha256="test",
        extractor_model="test",
        extractor_args_sha256="test",
    )


def test_preparation_failure_closes_environment_before_agent_creation(
    monkeypatch, tmp_path
):
    from tau2.data_model.run_config import AudioNativeConfig
    from tau2.data_model.simulation import VoiceRunConfig
    from tau2.domains.retail.environment import get_environment, get_tasks
    from tau2.runner import build

    env = get_environment()
    closed = []
    monkeypatch.setattr(env, "close", lambda: closed.append(True))
    monkeypatch.setattr(build, "build_environment", lambda *args, **kwargs: env)
    monkeypatch.setattr(
        build,
        "build_agent",
        lambda *args, **kwargs: pytest.fail("Agent startup before hotword preparation"),
    )

    def unavailable(**kwargs):
        raise RuntimeError("provider-private-secret")

    monkeypatch.setattr(asr_hotwords, "generate", unavailable)
    config = VoiceRunConfig(
        domain="retail",
        language="en",
        audio_native_config=AudioNativeConfig(
            provider="qwen_agent",
            qwen_agent_config=qwen_config(
                asr_hotwords=HotwordSettings(enabled=True, cache_dir=tmp_path)
            ),
        ),
        llm_user="offline-extractor",
    )
    with pytest.raises(RuntimeError, match="ASR hotword preparation failed") as error:
        build.build_voice_orchestrator(config, get_tasks()[0])
    assert closed == [True] and "private-secret" not in str(error.value)


@pytest.mark.parametrize("failure", ["http", "malformed", "provider_error"])
def test_qwen_client_does_not_expose_provider_error_bodies(failure):
    from tau2.voice.qwen_asr import transcribe_qwen

    async def scenario():
        def remote(request):
            if failure == "http":
                return httpx.Response(401, text="provider-private-secret")
            if failure == "malformed":
                return httpx.Response(200, text="provider-private-secret")
            return httpx.Response(
                200,
                json={"code": "InvalidApiKey", "message": "provider-private-secret"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            with pytest.raises(RuntimeError, match="Qwen ASR") as error:
                await transcribe_qwen(
                    client,
                    "https://dashscope.invalid",
                    "qwen-audio-3.1-asr-flash",
                    "key",
                    b"\x10\x10" * 8000,
                    8000,
                    "en",
                )
        assert "private-secret" not in str(error.value)

    asyncio.run(scenario())


def qwen_config(**kwargs):
    return QwenAgentConfig(
        asr_provider="qwen",
        asr_model="qwen-audio-3.1-asr-flash",
        asr_api_key_env="DASHSCOPE_API_KEY3",
        asr_base_url="https://dashscope.invalid",
        asr_segment_seconds=1,
        **kwargs,
    )


def test_qwen_observer_async_audio_bounds_fixed_words_recheck_and_usage(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY3", "test-observer-key")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def remote(request):
            body = json.loads(request.content)
            assert request.headers["authorization"] == "Bearer test-observer-key"
            assert body["parameters"]["vocabulary"] == {"Kåre Østensen": 4}
            data = body["input"]["messages"][0]["content"][0]["input_audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))) as wav:
                requests.append((wav.getframerate(), wav.getnframes()))
            if len(requests) == 1:
                started.set()
                await release.wait()
            return httpx.Response(
                200,
                json={
                    "output": {"text": "Kåre Østensen"},
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )

        config = qwen_config(asr_hotwords=HotwordSettings(enabled=True))
        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = QwenPlayedAudioTranscriber(
                config, client=client, hotwords=profile()
            )
            await observer.connect()
            try:
                await observer.send(b"\x10\x10" * 8000)
                await started.wait()
                assert observer.drain() == []
                await asyncio.wait_for(observer.send(bytes(1600)), 0.1)
                release.set()
                await observer._segments.join()
                snapshot = observer.drain()
                assert snapshot[0].end_ms == 1000
                await observer.send(b"\x20\x20" * 8000)
                await observer._segments.join()
                observer.drain()
                review = await observer.recheck(
                    "Kåre Østensen", [f.model_dump() for f in snapshot]
                )
                assert review["end_ms"] == 1000 and review["status"] == "ok"
                assert requests[-1][0] == 16000
                assert abs(requests[-1][1] - 16000) <= 1
                assert observer.drain() == []
                repeated = await observer.recheck(
                    "Kåre Østensen", [f.model_dump() for f in snapshot]
                )
                assert repeated["cached"] and len(requests) == 3
                summary = observer.summary()
                assert summary["provider"] == "qwen" and not summary["word_timestamps"]
                assert summary["hotwords"]["term_count"] == 1
                assert (
                    len(summary["usage"]) == 3
                    and summary["usage"][-1]["purpose"] == "recheck"
                )
            finally:
                await observer.close()

    asyncio.run(scenario())


def test_disabled_hotwords_omit_wire_vocabulary_and_unprepared_profiles_fail(
    monkeypatch,
):
    monkeypatch.setenv("DASHSCOPE_API_KEY3", "key")
    with pytest.raises(ValueError, match="prepared per-task"):
        QwenPlayedAudioTranscriber(
            qwen_config(asr_hotwords=HotwordSettings(enabled=True))
        ).validate_config()

    async def scenario():
        def remote(request):
            assert "vocabulary" not in json.loads(request.content)["parameters"]
            return httpx.Response(200, json={"output": {"text": "Hello"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
            observer = QwenPlayedAudioTranscriber(
                qwen_config(), client=client, hotwords=profile()
            )
            await observer.connect()
            try:
                await observer.send(b"\x10\x10" * 8000)
                await observer._segments.join()
                assert observer.drain()[0].text.strip() == "Hello"
                assert observer.summary()["hotwords"] == {"enabled": False}
            finally:
                await observer.close()

    asyncio.run(scenario())


def test_observer_credentials_and_vocabulary_do_not_enter_mimo_frontend_environment(
    tmp_path,
):
    private = tmp_path / "config.env"
    private.write_text("""LLM_API_KEY=frontend
LLM_API_URL=https://frontend.invalid/v1
LLM_MODEL=frontend
MIMO_API_KEY=mimo
GAIA2_BACKEND_API_KEY=backend
GAIA2_BACKEND_API_BASE=https://backend.invalid/v1
GAIA2_BACKEND_MODEL=backend
DASHSCOPE_API_KEY3=observer-private
QWEN_AUDIO_REALTIME_BASE_URL=wss://dashscope.aliyuncs.com/api-ws/v1/realtime
QWEN_AUDIO_REALTIME_PROVIDER=dashscope
""")
    config = qwen_config(
        config_env_file=str(private),
        speech_to_speech=SpeechToSpeechConfig(),
        asr_base_url_env="QWEN_AUDIO_REALTIME_BASE_URL",
        asr_hotwords=HotwordSettings(enabled=True),
    )
    environment = config.resolved_environment({})
    assert "observer-private" not in str(environment)
    assert "TAU_S2S_ASR_API_KEY" not in environment
    assert json.loads(environment["TAU_S2S_CONFIG"])["asr_model"] == "mimo-v2.5-asr"

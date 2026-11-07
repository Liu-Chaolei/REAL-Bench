"""Verify independent per-key request caps without concurrent dataset writers."""

import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from tau2.task_authoring.gaia2_series.schema import (
    AuthoringConfig,
    ModelProfile,
    RequestPolicy,
    ReviewDecision,
)


@pytest.mark.parametrize("limits", [{}, {"MIMO_API_KEY": 2, "MIMO_API_KEY2": 4}])
def test_each_key_is_capped_and_provenance_contains_only_reference(
    monkeypatch, tmp_path, limits
):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    env = tmp_path / "keys.env"
    env.write_text("MIMO_API_KEY=first-test-secret\nMIMO_API_KEY2=second-test-secret\n")
    config = AuthoringConfig(
        config_env_file=str(env),
        author=ModelProfile(model="openai/mimo-v2.6-pro"),
        reviewer=ModelProfile(model="openai/mimo-v2.6-pro"),
    )
    policy = RequestPolicy(
        api_key_pool=["MIMO_API_KEY", "MIMO_API_KEY2"],
        per_key_concurrency=6,
        per_key_concurrency_overrides=limits,
    )
    monkeypatch.setattr(generation, "_key_pool_counters", {})
    monkeypatch.setattr(generation, "_key_pool_semaphores", {})
    lock = threading.Lock()
    active, peak, counts = Counter(), Counter(), Counter()
    calls = []

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        key = kwargs["api_key"]
        with lock:
            active[key] += 1
            peak[key] = max(peak[key], active[key])
            counts[key] += 1
        time.sleep(0.05)
        with lock:
            active[key] -= 1
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    name: True
                    for name, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)

    def worker(_):
        token = generation.request_policy_override.set(policy)
        try:
            return generation.call_model(
                config, config.author, "rules", {}, ReviewDecision, Reader(), calls
            )
        finally:
            generation.request_policy_override.reset(token)

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(worker, range(24)))
    assert all(result.accepted for result in results)
    assert set(counts.values()) == {12}
    assert all(value <= 6 for value in peak.values())
    assert peak["first-test-secret"] == limits.get("MIMO_API_KEY", 6)
    assert peak["second-test-secret"] == limits.get("MIMO_API_KEY2", 6)
    assert {call["api_key_env"] for call in calls} == set(policy.api_key_pool)
    assert "test-secret" not in json.dumps(calls)


def test_key_pool_rejects_duplicate_credentials_and_bad_names(monkeypatch, tmp_path):
    from tau2.task_authoring.gaia2_series.generation import select_pooled_key

    env = tmp_path / "keys.env"
    env.write_text("TEST_MIMO_FIRST=same\nTEST_MIMO_SECOND=same\n")
    config = AuthoringConfig(
        config_env_file=str(env),
        author=ModelProfile(model="fake/a"),
        reviewer=ModelProfile(model="fake/b"),
    )
    with pytest.raises(ValueError, match="distinct"):
        select_pooled_key(
            config, RequestPolicy(api_key_pool=["TEST_MIMO_FIRST", "TEST_MIMO_SECOND"])
        )
    with pytest.raises(ValueError, match="variable names"):
        RequestPolicy(api_key_pool=["literal-key"])


def test_only_rate_limits_trigger_key_cooldown(monkeypatch):
    from tau2.task_authoring.gaia2_series import generation

    class Limited(Exception):
        status_code = 429

    assert generation.rate_limited(Limited("too many requests"))
    assert not generation.rate_limited(Limited("Token Plan 用量上限 2056"))
    assert not generation.rate_limited(RuntimeError("连接上游超时 RELAY_002"))
    monkeypatch.setattr(
        generation, "_key_cooldowns", {"first": time.monotonic() + 0.02}
    )
    generation.wait_for_key_cooldown("second")
    generation.wait_for_key_cooldown("first")


@pytest.mark.parametrize("exhaust_all", [False, True])
def test_exhausted_pool_keys_are_skipped_without_restarting_conversation(
    monkeypatch, tmp_path, exhaust_all
):
    from tau2.data_model.message import AssistantMessage
    from tau2.task_authoring.gaia2_series import generation
    from tau2.utils import llm_utils

    env = tmp_path / "pool.env"
    env.write_text("TEST_FIRST=first\nTEST_SECOND=second\n")
    config = AuthoringConfig(
        config_env_file=str(env),
        author=ModelProfile(model="openai/mimo-test"),
        reviewer=ModelProfile(model="openai/mimo-test"),
    )
    policy = RequestPolicy(api_key_pool=["TEST_FIRST", "TEST_SECOND"], attempts=1)
    monkeypatch.setattr(generation, "_disabled_pool_keys", set())
    monkeypatch.setattr(generation, "_key_pool_counters", {})
    monkeypatch.setattr(generation, "_exhausted_routes", set())
    requested = []

    class Reader:
        def tools(self):
            return []

    def generate(**kwargs):
        requested.append((kwargs["api_key"], id(kwargs["messages"])))
        if kwargs["api_key"] == "first" or exhaust_all:
            raise RuntimeError("余额或额度不足 [RELAY_101]")
        return AssistantMessage(
            role="assistant",
            content=json.dumps(
                {
                    name: True
                    for name, field in ReviewDecision.model_fields.items()
                    if field.annotation is bool
                }
            ),
        )

    monkeypatch.setattr(llm_utils, "generate", generate)
    calls = []
    token = generation.request_policy_override.set(policy)
    try:
        if exhaust_all:
            with pytest.raises(
                generation.ProviderQuotaExhausted, match="All configured"
            ):
                generation.call_model(
                    config, config.author, "rules", {}, ReviewDecision, Reader(), calls
                )
        else:
            for _ in range(2):
                result = generation.call_model(
                    config, config.author, "rules", {}, ReviewDecision, Reader(), calls
                )
                assert result.accepted
    finally:
        generation.request_policy_override.reset(token)
    assert [key for key, _ in requested] == (
        ["first", "second"] if exhaust_all else ["first", "second", "second"]
    )
    assert requested[0][1] == requested[1][1]
    assert calls[0]["key_disabled"] is True
    assert calls[0]["retryable"] is True
    assert not generation._exhausted_routes

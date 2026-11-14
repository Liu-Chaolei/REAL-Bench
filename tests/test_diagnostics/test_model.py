"""Timeout and retry behavior for saved diagnostics model calls."""

import pytest

from tau2.diagnostics.config import MetricsConfig
from tau2.diagnostics.model import RequestBudget, TauJsonModel


def test_non_timeout_is_not_retried(monkeypatch, tmp_path):
    model = TauJsonModel(
        MetricsConfig(timeout_seconds=0.01), "prediction", tmp_path, RequestBudget(4)
    )
    calls = []

    def once(prompt, schema, *, validate=None):
        calls.append(1)
        raise ValueError("bad request")

    monkeypatch.setattr(model, "_complete_once", once)
    with pytest.raises(ValueError, match="bad request"):
        model.complete_sync("prompt", {})
    assert len(calls) == 1


def test_sync_provider_timeout_retries_once(monkeypatch, tmp_path):
    model = TauJsonModel(MetricsConfig(), "prediction", tmp_path, RequestBudget(4))
    calls = []

    def once(prompt, schema, *, validate=None):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("provider timeout")
        return "ok"

    monkeypatch.setattr(model, "_complete_once", once)
    assert model.complete_sync("prompt", {}) == "ok"
    assert len(calls) == 2

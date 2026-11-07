"""Unlimited voice runs preserve natural stops and independent failure limits."""

from types import SimpleNamespace

import pytest

from tau2.data_model.run_config import AudioNativeConfig
from tau2.data_model.simulation import TerminationReason, VoiceRunConfig
from tau2.orchestrator.full_duplex_orchestrator import FullDuplexOrchestrator


def test_zero_conversation_limit_survives_configuration_round_trip():
    config = VoiceRunConfig(
        domain="mock", audio_native_config=AudioNativeConfig(max_steps_seconds=0)
    )
    restored = VoiceRunConfig.model_validate_json(config.model_dump_json())
    assert restored.audio_native_config.max_steps_seconds == 0
    assert restored.effective_max_steps is None
    assert restored.timeout is None
    assert AudioNativeConfig(max_steps_seconds=240).max_steps_ticks == 1200
    with pytest.raises(ValueError):
        AudioNativeConfig(max_steps_seconds=-1)


@pytest.mark.parametrize(
    ("limit", "errors", "expected"),
    [
        (None, 0, None),
        (1200, 0, TerminationReason.MAX_STEPS),
        (None, 10, TerminationReason.TOO_MANY_ERRORS),
    ],
)
def test_unlimited_ticks_do_not_bypass_independent_error_stop(limit, errors, expected):
    state = SimpleNamespace(
        step_count=100_000,
        max_steps=limit,
        num_errors=errors,
        max_errors=10,
        done=False,
        termination_reason=None,
        _check_timeout=lambda: None,
    )
    FullDuplexOrchestrator._check_termination(state)
    assert state.termination_reason == expected
    assert state.done == (expected is not None)


def test_unlimited_voice_batch_records_no_step_cap(tmp_path, monkeypatch):
    """Exercise batch validation and Info serialization without live model calls."""
    import uuid

    from tau2.data_model.simulation import SimulationRun
    from tau2.run import get_tasks
    from tau2.runner import batch

    calls = []

    def natural_stop(config, task, **kwargs):
        calls.append(task.id)
        assert config.effective_max_steps is None
        return SimulationRun(
            id=str(uuid.uuid4()),
            task_id=task.id,
            mode="full_duplex",
            start_time="2026-01-01T00:00:00",
            end_time="2026-01-01T00:05:00",
            duration=300,
            seed=kwargs.get("seed"),
            termination_reason=TerminationReason.USER_STOP,
            ticks=[],
        )

    monkeypatch.setattr(batch, "run_single_task", natural_stop)
    config = VoiceRunConfig(
        domain="mock",
        task_ids=["create_task_1"],
        num_trials=1,
        auto_resume=True,
        audio_native_config=AudioNativeConfig(max_steps_seconds=0),
    )
    results = batch.run_tasks(
        config,
        get_tasks("mock", task_ids=config.task_ids),
        save_path=tmp_path / "results.json",
        save_dir=tmp_path,
        console_display=False,
    )
    assert calls == ["create_task_1"]
    assert results.info.max_steps is None
    assert results.simulations[0].termination_reason == TerminationReason.USER_STOP
    resumed = batch.run_tasks(
        config,
        get_tasks("mock", task_ids=config.task_ids),
        save_path=tmp_path / "results.json",
        save_dir=tmp_path,
        console_display=False,
    )
    assert calls == ["create_task_1"]
    assert len(resumed.simulations) == 1

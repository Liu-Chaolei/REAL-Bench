"""Exercise service-only scaling through failures, recovery and process restart."""

from tau2.task_authoring.gaia2_series.full import concurrency_for
from tau2.task_authoring.gaia2_series.health import RequestHealth


def window(prefix, errors=0, endpoint="http://backup/v1", seconds=10):
    return [
        {
            "request_id": f"{prefix}-{i}",
            "api_base": endpoint,
            "capacity_error": i < errors,
            "error": "timeout" if i < errors else None,
            "elapsed_seconds": seconds,
        }
        for i in range(100)
    ]


def test_disjoint_bad_windows_halve_once_and_do_not_reuse_history():
    state = {}
    controller = RequestHealth(32, state)
    first = window("first", 6)
    assert controller.update(first, 32, 100) == 32
    assert controller.update(first, 32, 101) == 32
    assert controller.update(first + window("second", 6), 32, 102) == 16
    assert state["cooldown_until"] == 162
    assert controller.update(first + window("second", 6), 16, 163) == 16


def test_recovery_survives_restart_and_ramps_to_cap():
    state = {}
    controller = RequestHealth(32, state)
    assert controller.update(window("a", 10) + window("b", 10), 32, 1) == 16
    controller = RequestHealth(32, state)
    assert controller.update(window("cooldown"), 16, 10) == 16
    assert controller.update(window("c"), 16, 62) == 16
    assert controller.update(window("d"), 16, 63) == 32
    assert controller.update(window("e") + window("f"), 32, 125) == 32


def test_latency_and_local_errors_never_trigger_downscaling():
    calls = window("slow", seconds=599) + window("slow2", seconds=599)
    assert (
        concurrency_for(
            32,
            32,
            calls=calls,
            now=1,
            health_state={},
            completed=0,
            infrastructure_errors=99,
        )
        == 32
    )


def test_quota_switch_resets_windows_without_blaming_backup():
    state = {}
    controller = RequestHealth(32, state)
    assert (
        controller.update(window("primary", 10, endpoint="https://primary/v1"), 32, 1)
        == 32
    )
    quotas = [{**c, "quota_exhausted": True} for c in window("quota", 100)]
    assert controller.update(quotas + window("backup", 10), 32, 2) == 32
    assert state["bad_windows"] == 1
    assert controller.update(window("backup2", 10), 32, 3) == 16


def test_minimum_and_percentage_boundaries():
    controller = RequestHealth(32)
    assert controller.update(window("a", 5) + window("b", 5), 32, 1) == 32
    assert controller.update(window("c", 6) + window("d", 6), 32, 2) == 16
    assert controller.update(window("e", 6) + window("f", 6), 16, 63) == 8
    assert controller.update(window("g", 6) + window("h", 6), 8, 124) == 8
    assert controller.state["decisions"][-1]["reason"] == "unhealthy_at_minimum"


def test_single_spike_cannot_accumulate_across_healthy_window():
    controller = RequestHealth(32)
    assert controller.update(window("a", 50), 32, 1) == 32
    assert controller.update(window("b"), 32, 2) == 32
    assert controller.update(window("c", 50), 32, 3) == 32


def test_low_concurrency_profile_can_fall_back_to_four_two_and_one():
    controller = RequestHealth(8)
    assert controller.update(window("a", 6) + window("b", 6), 8, 1) == 4
    assert controller.update(window("c", 6) + window("d", 6), 4, 62) == 2
    assert controller.update(window("e", 6) + window("f", 6), 2, 123) == 1

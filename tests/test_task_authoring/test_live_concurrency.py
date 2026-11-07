"""The active batch must respond to worker-limit changes before it finishes."""

import threading

from tau2.task_authoring.gaia2_series import pipeline


def test_batch_scales_up_without_waiting_for_the_whole_batch(tmp_path):
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)
    active = 0
    peak = 0
    checks = 0

    def target():
        nonlocal checks
        checks += 1
        return 1 if checks == 1 else 3

    def worker(task_id):
        nonlocal active, peak
        if task_id == "first":
            return {"id": task_id, "status": "verified"}
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return {"id": task_id, "status": "verified"}

    token = pipeline.batch_concurrency_control.set((3, target))
    try:
        result = pipeline._batch(tmp_path, ["first", "a", "b", "c"], worker, 1, "live")
    finally:
        pipeline.batch_concurrency_control.reset(token)
    assert peak == 3
    assert len(result["results"]) == 4
    assert all(row["status"] == "verified" for row in result["results"])


def test_batch_reduction_drains_running_tasks_before_refilling(tmp_path):
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)
    active = 0
    peaks = []
    launched = []
    reduced = False

    def target():
        nonlocal reduced
        if len(launched) >= 3:
            reduced = True
            return 1
        return 3

    def worker(task_id):
        nonlocal active
        with lock:
            launched.append(task_id)
            active += 1
            if task_id in ["d", "e"]:
                peaks.append(active)
        if task_id in ["a", "b", "c"]:
            barrier.wait()
        with lock:
            active -= 1
        return {"id": task_id, "status": "verified"}

    token = pipeline.batch_concurrency_control.set((3, target))
    try:
        result = pipeline._batch(tmp_path, ["a", "b", "c", "d", "e"], worker, 3, "live")
    finally:
        pipeline.batch_concurrency_control.reset(token)
    assert reduced
    assert peaks == [1, 1]
    assert all(row["status"] == "verified" for row in result["results"])

"""Small shared task-equal averaging helpers for capability summaries."""

from collections import defaultdict
from statistics import mean


def task_mean(observations):
    """Average available trials within each task, then weight tasks equally."""
    tasks = defaultdict(list)
    for key, value in observations:
        if value is not None:
            tasks[key].append(value)
    return mean(mean(values) for values in tasks.values()) if tasks else None


def task_key(row):
    """Keep datasets/languages separate while pooling saved shards of one task."""
    source = row.get("summary", {}).get("source", {})
    metadata = row.get("metadata", {})
    return tuple(
        row.get(k) or metadata.get(k) or source.get(k)
        for k in ("domain", "language", "task_set_name")
    ) + (row.get("task_id") or metadata.get("task_id") or row.get("id"),)

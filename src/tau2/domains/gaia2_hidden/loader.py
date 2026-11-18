"""Loader for the independent Gaia2 hidden-intent task package."""

import json
from pathlib import Path
from typing import Optional

from tau2.data_model.tasks import Task

HIDDEN_DATA_DIR = (
    Path(__file__).resolve().parents[4] / "data" / "tau2" / "domains" / "gaia2_hidden"
)


def get_tasks(task_split_name: Optional[str] = None, **_: str) -> list[Task]:
    """Load hidden-intent tasks without touching the canonical Gaia2 set."""
    tasks = [
        Task.model_validate(item)
        for item in json.loads((HIDDEN_DATA_DIR / "tasks.json").read_text())
    ]
    if task_split_name is None:
        return tasks
    splits = get_tasks_split()
    if task_split_name not in splits:
        raise ValueError(f"Unknown gaia2_hidden split {task_split_name}")
    selected = set(splits[task_split_name])
    return [task for task in tasks if task.id in selected]


def get_tasks_split() -> dict[str, list[str]]:
    """Return the independent hidden-intent task splits."""
    return json.loads((HIDDEN_DATA_DIR / "split_tasks.json").read_text())


def get_hidden_plan(task_id: str) -> dict:
    """Load the private plan for one hidden task."""
    plans = json.loads((HIDDEN_DATA_DIR / "hidden_requirements.json").read_text())
    for plan in plans:
        if plan["task_id"] == task_id:
            return plan
    raise KeyError(f"Unknown hidden-intent task {task_id}")


def get_goal_graph(task_id: str) -> dict:
    """Load the private goal graph for one hidden task."""
    graphs = json.loads((HIDDEN_DATA_DIR / "goal_graphs.json").read_text())
    try:
        return graphs[task_id]
    except KeyError:
        raise KeyError(f"Unknown hidden-intent task {task_id}") from None

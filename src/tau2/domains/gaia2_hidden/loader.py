"""Loader for the independent Gaia2 hidden-intent task package."""

import json
from pathlib import Path
from typing import Optional

from tau2.data_model.tasks import Task

HIDDEN_DATA_DIR = (
    Path(__file__).resolve().parents[4] / "data" / "tau2" / "domains" / "gaia2_hidden"
)


def _data_dir(package: str = "gaia2_hidden") -> Path:
    """Resolve a packaged hidden-intent task set by its registered name."""
    if package == "gaia2_hidden":
        return HIDDEN_DATA_DIR
    raise ValueError(f"Unknown hidden-intent task package: {package}")


def _get_tasks(package: str, task_split_name: Optional[str] = None) -> list[Task]:
    data_dir = _data_dir(package)
    tasks = [
        Task.model_validate(item)
        for item in json.loads((data_dir / "tasks.json").read_text())
    ]
    if task_split_name is None:
        return tasks
    splits = _get_tasks_split(package)
    if task_split_name not in splits:
        raise ValueError(f"Unknown {package} split {task_split_name}")
    selected = set(splits[task_split_name])
    return [task for task in tasks if task.id in selected]


def _get_tasks_split(package: str) -> dict[str, list[str]]:
    return json.loads((_data_dir(package) / "split_tasks.json").read_text())


def get_tasks(task_split_name: Optional[str] = None, **_: str) -> list[Task]:
    """Load hidden-intent tasks without touching the canonical Gaia2 set."""
    return _get_tasks("gaia2_hidden", task_split_name)


def get_tasks_split() -> dict[str, list[str]]:
    """Return the independent hidden-intent task splits."""
    return _get_tasks_split("gaia2_hidden")


def get_hidden_plan(task_id: str, package: str = "gaia2_hidden") -> dict:
    """Load the private plan for one hidden task."""
    plans = json.loads((_data_dir(package) / "hidden_requirements.json").read_text())
    for plan in plans:
        if plan["task_id"] == task_id:
            return plan
    raise KeyError(f"Unknown hidden-intent task {task_id}")


def get_goal_graph(task_id: str, package: str = "gaia2_hidden") -> dict:
    """Load the private goal graph for one hidden task."""
    graphs = json.loads((_data_dir(package) / "goal_graphs.json").read_text())
    try:
        return graphs[task_id]
    except KeyError:
        raise KeyError(f"Unknown hidden-intent task {task_id}") from None

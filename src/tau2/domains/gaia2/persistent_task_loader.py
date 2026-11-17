"""GAIA2 integration helpers for the persistent-task benchmark sidecar."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tau2.data_model.scenario import ScenarioAction
from tau2.domains.gaia2.tools import Gaia2Tools
from tau2.environment.toolkit import ToolType, is_tool
from tau2.persistent_tasks.adapters import InMemoryTaskAdapter
from tau2.persistent_tasks.legacy.scheduler import PersistentTaskScheduler
from tau2.persistent_tasks.legacy.service import PersistentTaskService
from tau2.persistent_tasks.models import (
    PersistentTask,
    PersistentTaskRequest,
    ReleaseCondition,
    TaskStateEvent,
)
from tau2.persistent_tasks.trace import TaskTrace

PERSISTENT_TASKS_DIRNAME = "persistent_tasks"


class PersistentTaskTools(Gaia2Tools):
    """Expose task lifecycle tools alongside the regular GAIA2 tools."""

    def __init__(self, *args, task_adapter: InMemoryTaskAdapter, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_adapter = task_adapter

    def get_tools(self, include=None):
        """Merge persistent lifecycle tools with the ordinary Gaia2 tools."""
        from tau2.environment.tool import as_tool

        tools = super().get_tools(include)
        names = {
            "list_tasks",
            "query_task",
            "modify_task",
            "cancel_task",
            "get_task_result",
        }
        own = {name: as_tool(getattr(self, name)) for name in names}
        if include is None:
            tools.update({name: own[name] for name in names})
        else:
            tools.update({name: own[name] for name in names if name in include})
        return tools

    @is_tool(ToolType.READ)
    def list_tasks(self, request_id: str | None = None) -> list[dict[str, Any]]:
        """List the persistent tasks available in the current stage.

        Args:
            request_id: Optional evaluator request identifier.
        """
        return [
            task.model_dump(mode="json")
            for task in self.task_adapter.list_tasks(request_id)
        ]

    @is_tool(ToolType.READ)
    def query_task(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> dict[str, Any]:
        """Query one persistent task by its internal task identifier.

        Args:
            source_task_id: The original Gaia2 task ID.
            source_stage: The original Gaia2 stage.
            request_id: Optional evaluator request identifier.
        """
        return self.task_adapter.query_task(
            source_task_id, source_stage, request_id
        ).model_dump(mode="json")

    @is_tool(ToolType.WRITE)
    def modify_task(
        self,
        source_task_id: str,
        source_stage: int,
        changes: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Modify fields allowed by the current persistent task.

        Args:
            source_task_id: The original Gaia2 task ID.
            source_stage: The original Gaia2 stage.
            changes: JSON fields and values to update.
            request_id: Optional evaluator request identifier.
        """
        return self.task_adapter.modify_task(
            source_task_id, source_stage, changes, request_id
        ).model_dump(mode="json")

    @is_tool(ToolType.WRITE)
    def cancel_task(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> dict[str, Any]:
        """Cancel a waiting or running persistent task.

        Args:
            source_task_id: The original Gaia2 task ID.
            source_stage: The original Gaia2 stage.
            request_id: Optional evaluator request identifier.
        """
        return self.task_adapter.cancel_task(
            source_task_id, source_stage, request_id
        ).model_dump(mode="json")

    @is_tool(ToolType.READ)
    def get_task_result(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> dict[str, Any]:
        """Read the result of a completed persistent task.

        Args:
            source_task_id: The original Gaia2 task ID.
            source_stage: The original Gaia2 stage.
            request_id: Optional evaluator request identifier.
        """
        return self.task_adapter.get_task_result(
            source_task_id, source_stage, request_id
        ).model_dump(mode="json")


def persistent_tasks_root(root: Path) -> Path:
    """Return the sidecar directory below a GAIA2 data root."""
    return Path(root) / PERSISTENT_TASKS_DIRNAME


def load_persistent_scenario(scenario_id: str, *, root: Path) -> dict[str, Any]:
    """Load one sidecar scenario and validate its task ledger."""
    directory = persistent_tasks_root(root)
    tasks = {row["scenario_id"]: row for row in _read(directory / "tasks.json")}
    references = {
        row["scenario_id"]: row for row in _read(directory / "references.json")
    }
    scoring = {row["scenario_id"]: row for row in _read(directory / "scoring.json")}
    triggers = {row["scenario_id"]: row for row in _read(directory / "triggers.json")}
    missing = [
        name
        for name, values in (
            ("tasks", tasks),
            ("references", references),
            ("scoring", scoring),
            ("triggers", triggers),
        )
        if scenario_id not in values
    ]
    if missing:
        raise ValueError(
            f"Persistent task scenario {scenario_id} is missing: {', '.join(missing)}"
        )
    scenario = {
        "task": tasks[scenario_id],
        "references": references[scenario_id],
        "scoring": scoring[scenario_id],
        "triggers": triggers[scenario_id],
    }
    validate_source_bindings(scenario, root=root)
    return scenario


def load_persistent_for_target(
    source_task_id: str, source_stage: int, *, root: Path
) -> dict[str, Any] | None:
    """Load the sidecar scenario attached to one native task/stage, if any."""
    directory = persistent_tasks_root(root)
    path = directory / "tasks.json"
    if not path.exists():
        return None
    rows = _read(path)
    match = next(
        (
            row
            for row in rows
            if row["source_task_id"] == source_task_id
            and row["source_stage"] == source_stage
        ),
        None,
    )
    return load_persistent_scenario(match["scenario_id"], root=root) if match else None


def validate_source_bindings(scenario: dict[str, Any], *, root: Path) -> None:
    """Ensure every persistent activation points into the source event graph."""
    source_path = Path(root) / "tasks.json"
    source_tasks = {row["id"]: row for row in json.loads(source_path.read_text())}
    content = scenario["task"]
    source = source_tasks.get(content["source_task_id"])
    if source is None:
        raise ValueError(f"Unknown source Gaia2 task: {content['source_task_id']}")
    event_map = {event["id"]: event for event in source["scenario"]["events"]}
    stage = content["source_stage"]
    stage_ids = set(source["scenario"]["stages"][stage]["event_ids"])
    source_ids = set(content.get("source_event_ids", []))
    if not source_ids <= stage_ids or not source_ids <= set(event_map):
        raise ValueError(
            f"Persistent scenario {content['scenario_id']} binds an event outside its source stage"
        )
    trigger_ids = {
        event["id"] for event in scenario["triggers"].get("state_events", [])
    }
    for event in scenario["triggers"].get("state_events", []):
        dependencies = set(event.get("depends_on", []))
        if not dependencies <= source_ids | trigger_ids:
            raise ValueError(
                f"Persistent state event {event['id']} has an unknown dependency"
            )


def build_task_service(scenario: dict[str, Any]) -> PersistentTaskService:
    """Build a projection for the original Gaia2 task/stage target."""
    content = scenario["task"]
    task = PersistentTask(
        source_task_id=content["source_task_id"],
        source_stage=content["source_stage"],
        title=f"Gaia2 task {content['source_task_id']}",
        summary="Original Gaia2 stage workflow",
        state="waiting",
        progress={
            "current_step": "Waiting for original Gaia2 events",
            "completed_steps": 0,
            "total_steps": 0,
        },
        result=None,
        mutable_fields={"summary", "progress"},
    )
    return PersistentTaskService([task], trace=TaskTrace())


def build_task_tools(
    snapshot,
    *,
    assets: Path,
    scenario: dict[str, Any],
    clock=None,
    service: PersistentTaskService | None = None,
) -> PersistentTaskTools:
    """Create a Gaia2 toolkit with an isolated persistent-task adapter."""
    service = service or build_task_service(scenario)
    return PersistentTaskTools(
        snapshot,
        assets=assets,
        clock=clock,
        task_adapter=InMemoryTaskAdapter(service),
    )


def build_scheduler(
    scenario: dict[str, Any], service: PersistentTaskService
) -> PersistentTaskScheduler:
    """Build a scheduler from native-task state events and release records."""
    events = [
        TaskStateEvent.model_validate(event)
        for event in scenario["triggers"].get("state_events", [])
    ]
    requests = [
        PersistentTaskRequest(
            request_id=release["request_id"],
            text=next(
                item["text"]
                for item in scenario["task"]["requests"]
                if item["request_id"] == release["request_id"]
            ),
            release_condition=ReleaseCondition(**release["condition"]),
        )
        for release in scenario["triggers"].get("releases", [])
    ]
    return PersistentTaskScheduler(service, events, requests)


def compile_persistent_actions(
    scenario: dict[str, Any], *, source_task: dict[str, Any]
) -> list[ScenarioAction]:
    """Compile sidecar state/release records into private scenario actions.

    The returned actions are appended by an integration runner to the selected
    source stage. They retain dependencies on original GAIA2 event IDs; the
    scheduler consumes the private state actions and the normal user-input path
    consumes the generated user actions.
    """
    validate_source_bindings(scenario, root=Path("data/tau2/domains/gaia2"))
    source_ids = {event["id"] for event in source_task["scenario"]["events"]}
    actions: list[ScenarioAction] = []
    state_ids = set()
    for event in scenario["triggers"].get("state_events", []):
        dependencies = list(event.get("depends_on", []))
        if not set(dependencies) <= source_ids | state_ids:
            raise ValueError(f"Unknown persistent event dependency: {event['id']}")
        state_ids.add(event["id"])
        actions.append(
            ScenarioAction(
                id=event["id"],
                kind="environment",
                tool="PersistentTask__state_event",
                arguments=event,
                dependencies=dependencies,
                delay_seconds=event.get("delay_seconds", 0),
            )
        )
    for release in scenario["triggers"].get("releases", []):
        dependencies = list(release.get("depends_on", []))
        if not set(dependencies) <= source_ids | state_ids:
            raise ValueError(
                f"Unknown persistent release dependency: {release['request_id']}"
            )
        request = next(
            item
            for item in scenario["task"]["requests"]
            if item["request_id"] == release["request_id"]
        )
        actions.append(
            ScenarioAction(
                id=release["request_id"],
                kind="user",
                tool="AgentUserInterface__send_message_to_agent",
                arguments={"content": request["text"]},
                dependencies=dependencies,
                delay_seconds=release.get("delay_seconds", 0),
            )
        )
    return actions


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise ValueError(f"Persistent task file must contain a list: {path}")
    return value

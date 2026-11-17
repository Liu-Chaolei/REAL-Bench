"""In-memory task service used by the first persistent-task benchmark version."""

from __future__ import annotations

import copy
from typing import Any

from tau2.persistent_tasks.models import (
    PersistentTask,
    TaskProgress,
    TaskResult,
    TaskState,
)
from tau2.persistent_tasks.trace import TaskTrace


class PersistentTaskService:
    """Project one original Gaia2 task stage and record lifecycle operations."""

    def __init__(self, tasks: list[PersistentTask], *, trace: TaskTrace | None = None):
        identifiers = [
            self._key(task.source_task_id, task.source_stage) for task in tasks
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate persistent task IDs")
        self._tasks = {
            self._key(task.source_task_id, task.source_stage): copy.deepcopy(task)
            for task in tasks
        }
        self.trace = trace or TaskTrace()
        self.active_request_id: str | None = None

    def set_active_request(self, request_id: str | None) -> None:
        """Bind subsequent unlabelled operations to the delivered request."""
        self.active_request_id = request_id

    def _effective_request_id(self, request_id: str | None) -> str | None:
        """Prefer the runtime request window over an optional tool argument."""
        return self.active_request_id or request_id

    def list_tasks(self, *, request_id: str | None = None) -> list[PersistentTask]:
        """Return a stable, deep-copied task view."""
        result = [
            copy.deepcopy(self._tasks[key]) for key in sorted(self._tasks) if True
        ]
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="list",
            result=result,
        )
        return result

    def query_task(
        self, source_task_id: str, source_stage: int, *, request_id: str | None = None
    ) -> PersistentTask:
        """Return one task and record the read."""
        task = self._get(source_task_id, source_stage)
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="query",
            source_task_id=source_task_id,
            source_stage=source_stage,
            state_before=task.state,
            state_after=task.state,
            result=task,
        )
        return copy.deepcopy(task)

    def modify_task(
        self,
        source_task_id: str,
        source_stage: int,
        changes: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> PersistentTask:
        """Modify only explicitly mutable task fields."""
        task = self._get(source_task_id, source_stage)
        before = task.state
        if task.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            raise ValueError(
                f"cannot modify terminal task {source_task_id}:{source_stage}"
            )
        unknown = set(changes) - task.mutable_fields
        if unknown:
            raise ValueError(
                f"fields are not mutable for {source_task_id}:{source_stage}: {sorted(unknown)}"
            )
        updated = task.model_copy(deep=True)
        for field, value in changes.items():
            if field == "progress":
                value = TaskProgress.model_validate(value)
            setattr(updated, field, value)
        updated = PersistentTask.model_validate(updated.model_dump(mode="python"))
        self._tasks[self._key(source_task_id, source_stage)] = updated
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="modify",
            source_task_id=source_task_id,
            source_stage=source_stage,
            arguments=copy.deepcopy(changes),
            state_before=before,
            state_after=updated.state,
            result=updated,
        )
        return copy.deepcopy(updated)

    def cancel_task(
        self, source_task_id: str, source_stage: int, *, request_id: str | None = None
    ) -> PersistentTask:
        """Request and immediately confirm cancellation for the in-memory backend."""
        task = self._get(source_task_id, source_stage)
        before = task.state
        if task.state == TaskState.COMPLETED:
            raise ValueError(
                f"cannot cancel completed task {source_task_id}:{source_stage}"
            )
        if task.state == TaskState.CANCELLED:
            self.trace.operation(
                request_id=self._effective_request_id(request_id),
                operation="cancel",
                source_task_id=source_task_id,
                source_stage=source_stage,
                state_before=before,
                state_after=before,
                result=task,
            )
            return copy.deepcopy(task)
        self.request_cancel(source_task_id, source_stage, request_id=request_id)
        return self.confirm_cancel(source_task_id, source_stage, request_id=request_id)

    def request_cancel(
        self, source_task_id: str, source_stage: int, *, request_id: str | None = None
    ) -> PersistentTask:
        """Mark a task for cancellation before pending work is drained."""
        task = self._get(source_task_id, source_stage)
        if task.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            return copy.deepcopy(task)
        updated = task.model_copy(update={"state": TaskState.CANCEL_REQUESTED})
        self._tasks[self._key(source_task_id, source_stage)] = updated
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="cancel_requested",
            source_task_id=source_task_id,
            source_stage=source_stage,
            state_before=task.state,
            state_after=updated.state,
            result=updated,
        )
        return copy.deepcopy(updated)

    def confirm_cancel(
        self, source_task_id: str, source_stage: int, *, request_id: str | None = None
    ) -> PersistentTask:
        """Commit cancellation after pending native work is stopped."""
        task = self._get(source_task_id, source_stage)
        if task.state == TaskState.COMPLETED:
            raise ValueError(
                f"cannot cancel completed task {source_task_id}:{source_stage}"
            )
        if task.state == TaskState.CANCELLED:
            return copy.deepcopy(task)
        updated = task.model_copy(update={"state": TaskState.CANCELLED, "result": None})
        self._tasks[self._key(source_task_id, source_stage)] = updated
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="cancelled",
            source_task_id=source_task_id,
            source_stage=source_stage,
            state_before=task.state,
            state_after=updated.state,
            result=updated,
        )
        return copy.deepcopy(updated)

    def get_task_result(
        self, source_task_id: str, source_stage: int, *, request_id: str | None = None
    ) -> TaskResult:
        """Return a completed result."""
        task = self._get(source_task_id, source_stage)
        if task.state != TaskState.COMPLETED or task.result is None:
            raise ValueError(
                f"task {source_task_id}:{source_stage} has no completed result"
            )
        self.trace.operation(
            request_id=self._effective_request_id(request_id),
            operation="result",
            source_task_id=source_task_id,
            source_stage=source_stage,
            state_before=task.state,
            state_after=task.state,
            result=task.result,
        )
        return copy.deepcopy(task.result)

    def apply_event(
        self, event, *, now: float = 0, completed_events: set[str] | None = None
    ) -> PersistentTask:
        """Apply one scheduled state event, respecting terminal states."""
        completed_events = completed_events or set()
        if not set(event.dependencies) <= completed_events:
            raise ValueError(f"event dependencies are not complete: {event.id}")
        task = self._get(event.source_task_id, event.source_stage)
        if task.state in {
            TaskState.CANCELLED,
            TaskState.CANCEL_REQUESTED,
            TaskState.FAILED,
            TaskState.UNKNOWN,
        }:
            return copy.deepcopy(task)
        if task.state == TaskState.COMPLETED and event.new_state != TaskState.COMPLETED:
            return copy.deepcopy(task)
        progress = event.progress or task.progress
        result = event.result if event.new_state == TaskState.COMPLETED else None
        allowed = {
            TaskState.WAITING: {
                TaskState.WAITING,
                TaskState.RUNNING,
                TaskState.COMPLETED,
                TaskState.CANCELLED,
            },
            TaskState.RUNNING: {
                TaskState.RUNNING,
                TaskState.COMPLETED,
                TaskState.CANCELLED,
            },
            TaskState.COMPLETED: {TaskState.COMPLETED},
            TaskState.CANCELLED: {TaskState.CANCELLED},
        }
        if event.new_state not in allowed[task.state]:
            raise ValueError(
                f"invalid task transition: {task.state} -> {event.new_state}"
            )
        updated = PersistentTask.model_validate(
            task.model_copy(
                update={
                    "state": event.new_state,
                    "completed_milestones": task.completed_milestones
                    | ({event.milestone_id} if event.milestone_id else set()),
                    "progress": progress,
                    "result": result,
                }
            ).model_dump(mode="python")
        )
        self._tasks[self._key(event.source_task_id, event.source_stage)] = updated
        self.trace.operation(
            operation="state_event",
            source_task_id=event.source_task_id,
            source_stage=event.source_stage,
            arguments={"event_id": event.id},
            state_before=task.state,
            state_after=updated.state,
            result=updated,
            started_at=now,
            completed_at=now,
        )
        return copy.deepcopy(updated)

    def invalidate_pending(self, source_task_id: str, source_stage: int) -> None:
        """Prevent future state events from advancing a cancelled task."""
        for key, event in list(self._tasks.items()):
            if (
                event.source_task_id == source_task_id
                and event.source_stage == source_stage
            ):
                self.trace.operation(
                    operation="pending_invalidated",
                    source_task_id=source_task_id,
                    source_stage=source_stage,
                )

    def snapshot(self) -> list[PersistentTask]:
        """Return a deep copy for evaluation or replay."""
        return [copy.deepcopy(self._tasks[key]) for key in sorted(self._tasks)]

    @staticmethod
    def _key(source_task_id: str, source_stage: int) -> str:
        return f"{source_task_id}:{source_stage}"

    def _get(self, source_task_id: str, source_stage: int) -> PersistentTask:
        try:
            return self._tasks[self._key(source_task_id, source_stage)]
        except KeyError:
            raise ValueError(
                f"unknown persistent task: {source_task_id}:{source_stage}"
            ) from None

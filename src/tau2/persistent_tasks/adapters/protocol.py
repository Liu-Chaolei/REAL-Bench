"""Provider-neutral task adapter contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.models import PersistentTask, TaskResult


class TaskAdapter(Protocol):
    """Map one task backend to the benchmark task/stage protocol."""

    def list_tasks(self, request_id: str | None = None) -> list[PersistentTask]: ...

    def query_task(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> PersistentTask: ...

    def modify_task(
        self,
        source_task_id: str,
        source_stage: int,
        changes: dict[str, Any],
        request_id: str | None = None,
    ) -> PersistentTask: ...

    def cancel_task(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> PersistentTask: ...

    def get_task_result(
        self, source_task_id: str, source_stage: int, request_id: str | None = None
    ) -> TaskResult: ...


class BackendEventAdapter(Protocol):
    """Normalize provider events without interpreting benchmark semantics."""

    provider: str

    def normalize_event(
        self, payload: Mapping[str, Any]
    ) -> list[BackendTaskObservation]: ...

    def events(self, payloads): ...

    def backend_observation(self, payload): ...


class AsyncEvidenceSource(Protocol):
    """Passive, session-scoped evidence port exposed by a tested Agent."""

    def enable(self) -> None: ...

    def get_capabilities(self) -> dict: ...

    def drain_events(self) -> list[BackendTaskObservation]: ...

    def get_status(self) -> dict: ...

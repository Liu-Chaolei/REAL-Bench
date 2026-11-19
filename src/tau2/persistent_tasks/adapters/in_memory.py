"""In-memory adapter used by the benchmark environment and offline tests."""

from __future__ import annotations

from tau2.persistent_tasks.service import PersistentTaskService


class InMemoryTaskAdapter:
    """Expose a benchmark task service through the adapter protocol."""

    def __init__(self, service: PersistentTaskService):
        self.service = service

    def list_tasks(self, request_id=None):
        return self.service.list_tasks(request_id=request_id)

    def query_task(self, source_task_id, source_stage, request_id=None):
        return self.service.query_task(
            source_task_id, source_stage, request_id=request_id
        )

    def modify_task(self, source_task_id, source_stage, changes, request_id=None):
        return self.service.modify_task(
            source_task_id, source_stage, changes, request_id=request_id
        )

    def cancel_task(self, source_task_id, source_stage, request_id=None):
        return self.service.cancel_task(
            source_task_id, source_stage, request_id=request_id
        )

    def get_task_result(self, source_task_id, source_stage, request_id=None):
        return self.service.get_task_result(
            source_task_id, source_stage, request_id=request_id
        )

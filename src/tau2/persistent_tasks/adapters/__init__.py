"""Agent-facing task adapters.

Adapters translate a concrete task backend into the persistent-task protocol;
the task service, scheduler and evaluator remain provider-independent.
"""

from tau2.persistent_tasks.adapters.in_memory import InMemoryTaskAdapter
from tau2.persistent_tasks.adapters.protocol import BackendEventAdapter, TaskAdapter

__all__ = ["InMemoryTaskAdapter", "TaskAdapter"]
from tau2.persistent_tasks.adapters.gpt_live import GPTLiveEventAdapter
from tau2.persistent_tasks.adapters.qwen import QwenEventAdapter

__all__ = [
    "BackendEventAdapter",
    "GPTLiveEventAdapter",
    "InMemoryTaskAdapter",
    "QwenEventAdapter",
    "TaskAdapter",
]

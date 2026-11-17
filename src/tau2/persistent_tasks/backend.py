"""Compatibility exports for persistent-task backend primitives.

Canonical implementations live in ``core`` and ``triggers``.
"""

from tau2.persistent_tasks.core.identity import TaskIdentityBinder
from tau2.persistent_tasks.core.models import (
    BackendRequest,
    BackendTaskObservation,
    CanonicalState,
    StateTransition,
    TaskIdentity,
)
from tau2.persistent_tasks.core.projection import BackendTaskProjector
from tau2.persistent_tasks.triggers.scheduler import BackendTriggerScheduler

__all__ = [
    "BackendRequest",
    "BackendTaskObservation",
    "CanonicalState",
    "StateTransition",
    "TaskIdentity",
    "TaskIdentityBinder",
    "BackendTaskProjector",
    "BackendTriggerScheduler",
]

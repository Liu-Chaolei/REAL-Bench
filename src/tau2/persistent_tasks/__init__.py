"""Shared passive backend evidence contracts; legacy task execution is retired."""

from tau2.persistent_tasks.core.models import BackendTaskObservation, CanonicalState

__all__ = ["BackendTaskObservation", "CanonicalState"]

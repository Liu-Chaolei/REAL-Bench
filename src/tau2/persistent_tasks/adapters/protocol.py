"""Provider-neutral task adapter contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from tau2.persistent_tasks.core.models import BackendTaskObservation


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

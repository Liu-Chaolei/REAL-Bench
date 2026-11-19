"""Import-boundary checks for the canonical persistent-task layers."""

from tau2.persistent_tasks.backend import BackendTaskObservation as LegacyObservation
from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.core.overlays import BusinessOverlay
from tau2.persistent_tasks.runtime.evidence import write_summary
from tau2.persistent_tasks.runtime.replay import replay


def test_compatibility_paths_point_to_canonical_runtime_types():
    assert LegacyObservation is BackendTaskObservation
    assert BusinessOverlay.__module__ == "tau2.persistent_tasks.core.overlays"
    assert replay.__module__ == "tau2.persistent_tasks.runtime.replay"
    assert write_summary.__module__ == "tau2.persistent_tasks.runtime.evidence"


def test_legacy_runtime_aliases_keep_the_canonical_coordinator():
    from tau2.persistent_tasks.backend_runtime import BackendTaskRuntime
    from tau2.persistent_tasks.runtime.coordinator import PersistentTaskCoordinator

    assert BackendTaskRuntime is PersistentTaskCoordinator

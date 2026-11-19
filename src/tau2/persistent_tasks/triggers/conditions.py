"""Named trigger condition helpers.

The scheduler owns lifecycle transitions; this module provides small reusable
checks for callers that need to inspect a release condition.
"""

from tau2.persistent_tasks.core.models import CanonicalState


def state_matches(actual: CanonicalState | None, expected: CanonicalState) -> bool:
    """Return whether a projected state satisfies a request condition."""
    return actual == expected

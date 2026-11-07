"""Bounded candidate diagnostics, distinct from authoring infrastructure failures."""

import json
from contextlib import contextmanager


def excerpt(value, limit=4000):
    """Describe only the relevant tool return with bounded text and visible keys."""
    return {
        "type": type(value).__name__,
        "keys": list(value)[:30] if isinstance(value, dict) else None,
        "sample": json.dumps(value, ensure_ascii=False, default=str)[:limit],
    }


class CandidateValidationError(ValueError):
    """A generated candidate can be repaired using this concrete diagnostic."""

    def __init__(self, message, *, phase, stage=None, **details):
        super().__init__(message)
        self.details = {
            "failure_class": "candidate",
            "kind": "candidate_validation_failure",
            "phase": phase,
            "stage": stage,
            "message": message,
            **details,
        }


class VerificationInfrastructureError(RuntimeError):
    """An execution-system failure must not cause the model to rewrite a task."""


@contextmanager
def candidate_boundary(phase, **details):
    """Interpret model-supplied values without mistaking bad input for engine failure."""
    try:
        yield
    except (CandidateValidationError, VerificationInfrastructureError):
        raise
    except Exception as error:
        raise CandidateValidationError(
            f"{type(error).__name__}: {error}", phase=phase, **details
        ) from error


@contextmanager
def system_boundary(phase):
    """Give failures in trusted resource/environment operations a system boundary."""
    try:
        yield
    except VerificationInfrastructureError:
        raise
    except Exception as error:
        raise VerificationInfrastructureError(
            f"{phase}: {type(error).__name__}: {error}"
        ) from error

"""Application decorator compatibility; execution journals belong to tau2."""

from contextlib import contextmanager
from enum import Enum

from .tool_utils import OperationType


class EventType(str, Enum):
    AGENT = "AGENT"
    ENV = "ENV"
    USER = "USER"


def event_registered(operation_type=OperationType.READ, event_type=EventType.AGENT):
    """Preserve tool metadata without installing an independent event runtime."""

    def decorate(function):
        function.__event_registered__ = True
        function.__operation_type__ = operation_type
        return function

    return decorate


@contextmanager
def disable_events():
    """Nested application calls are journaled once at tau's tool boundary."""
    yield


class EventRegisterer:
    """Compatibility for the filesystem's nested application operations."""

    disable = staticmethod(disable_events)

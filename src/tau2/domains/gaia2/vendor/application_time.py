"""Trial-scoped defaults for application dataclasses that formerly used wall time."""

from contextlib import contextmanager
from contextvars import ContextVar

_clock = ContextVar("gaia2_application_clock", default=None)


def time():
    """Read the scenario's operation clock, including during object construction."""
    clock = _clock.get()
    return clock() if clock is not None else 0.0


@contextmanager
def use_clock(clock):
    """Keep concurrent trials and Python's global time module independent."""
    token = _clock.set(clock)
    try:
        yield
    finally:
        _clock.reset(token)

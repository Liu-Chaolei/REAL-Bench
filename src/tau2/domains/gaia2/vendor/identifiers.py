"""Trial-scoped deterministic identifiers for adapted application methods."""

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID
from uuid import uuid4 as _uuid4

_rng = ContextVar("gaia2_identifier_rng", default=None)


def uuid4():
    """Use an application's seeded generator during execution."""
    rng = _rng.get()
    return UUID(int=rng.getrandbits(128), version=4) if rng is not None else _uuid4()


@contextmanager
def use_rng(rng):
    """Keep concurrent trials and the rest of Python's UUID module isolated."""
    token = _rng.set(rng)
    try:
        yield
    finally:
        _rng.reset(token)

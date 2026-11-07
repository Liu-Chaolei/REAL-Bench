"""Serializable application state for Gaia2's native tau2 domain."""

from typing import Any

from pydantic import Field

from tau2.environment.db import DB


class Gaia2DB(DB):
    """The business state of a single independent scenario instance."""

    apps: dict[str, dict[str, Any]] = Field(default_factory=dict)

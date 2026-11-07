"""Read private dotenv settings without executing or serializing their values."""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv.parser import parse_stream


def read_config_environment(
    path: str | None, ambient: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Read literal settings; explicit environment values have precedence."""
    values = {}
    if path:
        with Path(path).expanduser().open() as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise ValueError(
                        f"Invalid dotenv syntax at line {binding.original.line}"
                    )
                if binding.key and binding.value is not None:
                    values[binding.key] = binding.value
    values.update(os.environ if ambient is None else ambient)
    return values


def resolve_env_reference(values: Mapping[str, str], name: str) -> str:
    """Expand selected references lazily, detecting missing settings and cycles."""

    def resolve(key, active=()):
        if key in active:
            raise ValueError(f"Cyclic configuration reference: {key}")
        if key not in values:
            raise ValueError(f"Missing environment variable {key}")
        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: resolve(match[1], (*active, key)),
            values[key],
        )

    return resolve(name)

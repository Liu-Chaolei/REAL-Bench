"""Functional text extracted from provider response transcript events."""

import re

_INTERNAL_DSML = re.compile(
    r"<DSML\s+calls>.*?</DSML\s+calls>", re.IGNORECASE | re.DOTALL
)


def sanitize_generated_text(value: str) -> str:
    """Remove provider-internal tool markup before communication scoring."""
    return " ".join(_INTERNAL_DSML.sub(" ", value or "").split())

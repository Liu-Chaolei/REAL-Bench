"""Persistent evidence hashing and sealed-file persistence."""

import hashlib
import json
from pathlib import Path


def evidence_digest(summary):
    """Return a stable hash independent of later judge verdicts."""
    return hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in summary.items()
                if key not in {"evidence_sha256", "grading"}
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def write_summary(summary, directory):
    """Save private evidence with restrictive permissions."""
    path = Path(directory) / "persistent_tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return path

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(canonical(row) + "\n" for row in records))
    temporary.replace(path)


def read_artifacts(path: Path) -> list[dict[str, Any]]:
    # Unicode separators can occur inside JSON strings; only LF ends a JSONL record.
    rows = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
    index_records(rows)
    return rows


def index_records(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        identity = row.get("id")
        if not isinstance(identity, str) or not identity or identity in result:
            raise ValueError("Record IDs must be nonempty, unique strings")
        result[identity] = row
    return result


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_get(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("Expected a JSON Pointer")
    current = document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("Invalid JSON Pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or str(int(token)) != token:
                raise ValueError("Invalid array index")
            current = current[int(token)]
        else:
            current = current[token]
    return current


def path_matches(pattern: str, path: str) -> bool:
    a, b = pattern.split("/"), path.split("/")
    return len(a) == len(b) and all(x == "*" or x == y for x, y in zip(a, b))

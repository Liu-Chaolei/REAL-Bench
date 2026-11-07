from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TextRecord:
    id: str
    text: str | None
    context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    unavailable_reason: str | None = None


class JsonModel(Protocol):
    async def complete(self, prompt: str, schema: dict[str, Any]) -> str: ...

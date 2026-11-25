"""Passive configuration embedded in historical OpenAI Live results."""

from pydantic import BaseModel, ConfigDict


class LiveConfig(BaseModel):
    """Saved settings only; no prompt rendering or provider execution."""

    model_config = ConfigDict(extra="forbid")

    backend_model: str
    voice: str = "marin"
    append_system_prompt: bool = True
    frontend_prompt: str | None = None
    backend_prompt: str | None = None

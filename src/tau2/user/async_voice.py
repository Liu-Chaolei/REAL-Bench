"""Private context for asynchronous caller generation and listener decisions."""

from concurrent.futures import Future
from random import Random
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tau2.data_model.message import TurnTakingAction

VoiceTaskOrigin = Literal[
    "normal", "interrupt", "backchannel", "tool_result", "listener", "scripted"
]


class PendingVoiceTask(BaseModel):
    """Keep an in-flight request and its original playback authorization."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    future: Future = Field(exclude=True)
    origin: VoiceTaskOrigin
    revision: int
    speech_epoch: int
    started_during_speech: bool
    started_tick: int
    input_count: int
    decision: TurnTakingAction
    rng: Random | None = Field(default=None, exclude=True)
    from_tool_result: bool = False
    discard_reason: str | None = None

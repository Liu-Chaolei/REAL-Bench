"""Configuration for the optional full-duplex semantic watchdog."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WatchdogConfig(BaseModel):
    """Optional semantic watchdog for long-running full-duplex calls."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False)
    initial_check_ticks: int = Field(default=6000, ge=1)
    check_interval_ticks: int = Field(default=3000, ge=1)
    max_total_ticks: int | None = Field(default=12000, ge=1)
    judge_timeout_seconds: float = Field(default=30.0, gt=0)
    heartbeat_timeout_seconds: float = Field(default=180.0, gt=0)
    context_ticks: int = Field(default=120, ge=10, le=1000)

    @model_validator(mode="after")
    def validate_total_cap(self):
        """Ensure the optional cap can reach the first watchdog checkpoint."""
        if (
            self.max_total_ticks is not None
            and self.max_total_ticks < self.initial_check_ticks
        ):
            raise ValueError("max_total_ticks must be at least initial_check_ticks")
        return self

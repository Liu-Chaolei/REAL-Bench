"""Application clock adapter; tau2 supplies the trial clock before execution."""


class TimeManager:
    """An initially frozen application clock, replaced by ScenarioRuntime."""

    def time(self):
        return 0.0

    def add_offset(self, value):
        raise RuntimeError("Applications cannot fast-forward the scenario clock")

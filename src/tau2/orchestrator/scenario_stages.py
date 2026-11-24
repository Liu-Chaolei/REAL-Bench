"""Control-signal-driven continuation of a single stateful conversation."""

from tau2.user.user_simulator_base import OUT_OF_SCOPE, STOP

SERIES_VERSION = "gaia2-series-runtime-v1"


def stage_requests(spec):
    """Return each stage's leading user event without including clarifications."""
    events = {event.id: event for event in spec.events}
    return [
        next(events[key] for key in stage.event_ids if events[key].kind == "user")
        for stage in spec.stages
    ]


class ScenarioStages:
    """Consume intermediate control signals; correctness is evaluated after the call."""

    def __init__(self, spec):
        self.requests = stage_requests(spec)
        self.current = 0
        self.transition_pending = False
        self.awaiting_delivery = True
        self.rows = [
            {
                "stage": stage.stage,
                "event_id": event.id,
                "queued": None,
                "started": None,
                "completed": None,
                "interruptions": [],
                "stops": [],
            }
            for stage, event in zip(spec.stages, self.requests)
        ]

    @staticmethod
    def position(runtime, index):
        return {
            "time": runtime.time(),
            "index": index,
            "sequence": len(runtime.records),
        }

    def stop(self, message, runtime, index):
        """Consume an intermediate STOP or out-of-scope signal.

        Both signals end the current scripted request and release the next one
        when a series has another stage.
        """
        text = message.content or ""
        if (
            message.is_tool_call()
            or not any(token in text for token in (STOP, OUT_OF_SCOPE))
        ):
            return False
        if self.transition_pending or self.awaiting_delivery:
            return True
        self.rows[self.current]["stops"].append(self.position(runtime, index))
        if self.current == len(self.requests) - 1:
            return False
        self.transition_pending = True
        return True

    def release(self, user, runtime, index):
        """Queue exactly one continuation once known output has drained."""
        hidden_runtime = getattr(user, "hidden_intent_runtime", None)
        if hidden_runtime is not None and self.current == 0:
            hidden_runtime.deactivate()
        observer = getattr(user, "diagnostic_recorder", None)
        if observer is not None:
            observer.stage(f"stage:{self.current}:end")
        user.scenario_transition_pending = False
        self.current += 1
        self.transition_pending = False
        self.awaiting_delivery = True
        event = self.requests[self.current]
        self.rows[self.current]["queued"] = self.position(runtime, index)
        user.queue_scenario_input(event.id, event.arguments["content"])

    def delivered(self, event_id, runtime, index, *, complete=False, when=None):
        """Record real transport rather than preparation of a request."""
        row = next((row for row in self.rows if row["event_id"] == event_id), None)
        if row is None:
            return
        position = self.position(runtime, index)
        if when is not None:
            position["time"] = when
        if row["started"] is None:
            row["started"] = position
        if complete:
            row["completed"] = position
        if row is self.rows[self.current]:
            # Do not accept another STOP before the whole request was spoken.
            self.awaiting_delivery = row["completed"] is None

    def interrupted(self, event_id, runtime, index):
        """Retain partial delivery when speech ends before its final chunk."""
        row = next((row for row in self.rows if row["event_id"] == event_id), None)
        if row is not None and row["completed"] is None:
            row["interruptions"].append(self.position(runtime, index))
            if row is self.rows[self.current]:
                return self.requests[self.current]
        return None

    def summary(self, reason):
        return {
            "version": SERIES_VERSION,
            "current_stage": self.current,
            "stages": self.rows,
            "termination_reason": reason,
            "not_reached": [
                row["stage"] for row in self.rows if row["started"] is None
            ],
        }

"""Request-only concurrency decisions with disjoint windows and resumable state."""

from datetime import datetime

from tau2.task_authoring.gaia2_series.storage import digest


class RequestHealth:
    """Candidate failures and local verification errors never enter these windows."""

    def __init__(self, maximum=32, state=None):
        self.maximum = maximum
        self.state = state if state is not None else {}
        self.state.setdefault("seen", [])
        self.state.setdefault("endpoint", None)
        self.state.setdefault("window", [])
        self.state.setdefault("bad_windows", 0)
        self.state.setdefault("good_windows", 0)
        self.state.setdefault("cooldown_until", 0)
        self.state.setdefault("decisions", [])

    def update(self, calls, current, now):
        """Consume each completed request once; apply at most one change per boundary."""
        seen = set(self.state["seen"])
        ordered = sorted(
            calls, key=lambda c: c.get("finished_at", c.get("started_at", ""))
        )
        selected = current
        changed = False
        for call in ordered:
            identifier = call.get("request_id") or digest(
                {
                    key: call.get(key)
                    for key in (
                        "started_at",
                        "request_sha256",
                        "provider_attempt",
                        "api_base",
                        "turn",
                    )
                }
            )
            if identifier in seen:
                continue
            seen.add(identifier)
            if call.get("quota_exhausted") or not call.get("api_base"):
                continue
            endpoint = call["api_base"]
            if endpoint != self.state["endpoint"]:
                self.state.update(
                    endpoint=endpoint, window=[], bad_windows=0, good_windows=0
                )
            completed_at = call.get("finished_at")
            when = (
                datetime.fromisoformat(completed_at).timestamp()
                if completed_at
                else now
            )
            if (
                when < self.state["cooldown_until"]
                or now < self.state["cooldown_until"]
                or changed
            ):
                continue
            self.state["window"].append(bool(call.get("capacity_error")))
            if len(self.state["window"]) < 100:
                continue
            rate = sum(self.state["window"]) / 100
            self.state["window"] = []
            self.state["bad_windows"] = (
                self.state["bad_windows"] + 1 if rate > 0.05 else 0
            )
            self.state["good_windows"] = (
                self.state["good_windows"] + 1 if rate <= 0.01 else 0
            )
            reason = None
            if self.state["bad_windows"] >= 2:
                minimum = 1 if self.maximum <= 8 else 8
                selected = max(minimum, current // 2)
                reason = (
                    "two_bad_request_windows"
                    if selected != current
                    else "unhealthy_at_minimum"
                )
            elif self.state["good_windows"] >= 2 and current < self.maximum:
                selected = min(self.maximum, current * 2)
                reason = "two_healthy_request_windows"
            if reason:
                self.state["decisions"].append(
                    {
                        "time": now,
                        "endpoint": endpoint,
                        "from": current,
                        "to": selected,
                        "error_rate": rate,
                        "reason": reason,
                    }
                )
                self.state.update(window=[], bad_windows=0, good_windows=0)
                if selected != current:
                    self.state["cooldown_until"] = now + 60
                    changed = True
        self.state["seen"] = sorted(seen)
        return selected

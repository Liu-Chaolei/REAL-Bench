"""Passive native-recovery observation. No reconnect or delivery commands."""

from __future__ import annotations

import re

VERSION = "gaia2-qwen-audio/6"
WATCHDOG_MESSAGE = "实时模型没有开始回复，语音连接已自动恢复，请再说一次。"
POLICY = {
    "recovery_mode": "observe_native_only",
    "watchdog_messages": [WATCHDOG_MESSAGE],
    "native_recovery_scope": "confirmed input; no unfinished playback or delivery",
    "recovery_budget": "remaining_original_task_budget",
    "adapter_reconnect": False,
    "adapter_redelivery": False,
}


class NativeRecovery:
    def __init__(self):
        self.episodes = []
        self.active = None

    @property
    def pending(self):
        return self.active is not None

    def start(self, now):
        if self.active is not None:
            self.active["ready_event_sec"] = None
            return False
        self.active = {
            "id": len(self.episodes) + 1,
            "started_sec": now,
            "ready_event_sec": None,
            "ready_confirmed_sec": None,
            "ended_sec": None,
            "outcome": "pending",
        }
        self.episodes.append(self.active)
        return True

    def connection(self, event, now):
        if not self.active:
            return
        kind, state = event.get("type"), event.get("state")
        if kind == "voice.connection" and state in {"connecting", "unavailable"}:
            self.active["ready_event_sec"] = None
        elif kind == "voice.ready" or (
            kind == "voice.connection" and state == "connected"
        ):
            self.active["ready_event_sec"] = now

    def confirm(self, state, now):
        if (
            not self.active
            or self.active["ready_event_sec"] is None
            or state.get("ready") is not True
        ):
            return None
        episode = self.active
        episode.update(ready_confirmed_sec=now, ended_sec=now, outcome="ready")
        self.active = None
        return dict(episode)

    def finish(self, reason, now):
        if self.active:
            self.active.update(ended_sec=now, outcome=reason)
            self.active = None

    def summary(self, now):
        return {
            "episodes": [dict(e) for e in self.episodes],
            "count": len(self.episodes),
            "completed_count": sum(e["outcome"] == "ready" for e in self.episodes),
            "duration_sec": sum(
                max(
                    0,
                    (e["ended_sec"] if e["ended_sec"] is not None else now)
                    - e["started_sec"],
                )
                for e in self.episodes
            ),
            "pending": self.pending,
        }


def external_service_cause(event):
    """Only explicit service evidence, never a guess from a local watchdog."""
    error = event.get("error") or {}
    if not isinstance(error, dict):
        error = {}
    code = error.get("code") or event.get("code")
    status = event.get("http_status") or event.get("status_code") or error.get("status")
    if not isinstance(code, str):
        code = None
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if not isinstance(status, int):
        status = None
    message = str(event.get("message") or error.get("message") or "")
    if status is None:
        match = re.search(
            r"\bHTTP(?:\s+(?:status|error))?\s*[:=]?\s*(401|402|403|429|5\d\d)\b",
            message,
            re.IGNORECASE,
        )
        if match:
            status = int(match[1])
    if code in {"insufficient_quota", "billing_hard_limit_reached"} or status == 402:
        return "provider_billing"
    if (
        code in {"invalid_api_key", "server_error"}
        or status in {401, 403}
        or (isinstance(status, int) and status >= 500)
    ):
        return "provider_unavailable"
    if code == "rate_limit_exceeded" or status == 429:
        return "provider_rate_limit"
    return None


def verify_recorded_recovery(root, result):
    """Reproduce recovery accounting from frozen events, without contacting qwen."""
    import json

    replayed = NativeRecovery()
    rows = [
        json.loads(line) for line in (root / "protocol.jsonl").read_text().splitlines()
    ]
    for row in rows:
        event = row["event"]
        if (
            event == "agent.runtime_event"
            and row.get("kind") == "response_start_watchdog"
            and replayed.pending
        ):
            replayed.start(row["elapsed_sec"])
        elif event == "native.recovery.started":
            if not replayed.start(row["started_sec"]):
                raise ValueError("Duplicate recovery admission")
            if replayed.episodes[-1]["id"] != row["episode_id"]:
                raise ValueError("Recovery identity differs")
        elif event == "native.recovery.progress":
            replayed.connection(row["connection_event"], row["observed_sec"])
        elif event == "native.recovery.ready_confirmed":
            if (
                replayed.active is None
                or replayed.active["ready_event_sec"] is None
                or row["readiness_probe_started_sec"]
                < replayed.active["ready_event_sec"]
            ):
                raise ValueError("Readiness snapshot predates the connection event")
            episode = replayed.confirm(
                {"ready": row.get("ready_snapshot")}, row["ready_confirmed_sec"]
            )
            if not episode or any(row.get(k) != v for k, v in episode.items()):
                raise ValueError(
                    "Recovery readiness lacks matching connection evidence"
                )
        elif event == "native.recovery.ended":
            if not replayed.pending:
                raise ValueError("Recovery ended without admission")
            replayed.finish(row["outcome"], row["ended_sec"])
            if any(row.get(k) != v for k, v in replayed.episodes[-1].items()):
                raise ValueError("Recovery ending differs")
    saved = json.loads((root / "native-recovery.json").read_text())
    summary = replayed.summary(result["timing"]["observation_end_sec"])
    if replayed.pending or summary != saved or saved != result.get("native_recovery"):
        raise ValueError("Recovery accounting differs from frozen evidence")
    if saved != result["timing"].get("native_recovery"):
        raise ValueError("Recovery metrics differ")
    return saved

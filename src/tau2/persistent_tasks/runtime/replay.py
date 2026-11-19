"""Deterministic replay of sealed persistent-task evidence."""

from tau2.persistent_tasks.config import PersistentTaskConfig
from tau2.persistent_tasks.core.models import BackendTaskObservation
from tau2.persistent_tasks.runtime.evidence import evidence_digest


def replay(summary):
    """Replay recorded normalized events; no models, clock or business tools."""
    from tau2.persistent_tasks.runtime.coordinator import PersistentTaskCoordinator

    if evidence_digest(summary) != summary["evidence_sha256"]:
        raise ValueError("persistent evidence hash mismatch")
    runtime = PersistentTaskCoordinator(
        PersistentTaskConfig.model_validate(summary["config"])
    )
    for item in summary["journal"]:
        kind = item["kind"]
        if kind == "observation":
            observation = BackendTaskObservation(**item["observation"])
            if summary.get("evidence_version") != "persistent-task-evidence-v1":
                from tau2.persistent_tasks.legacy.evidence import legacy_observation

                observation = legacy_observation(observation)
            runtime.observe(observation)
        elif kind == "capabilities":
            runtime.record_capabilities(item["capabilities"])
        elif kind == "channel_status":
            runtime.record_channel_status(item["status"])
        elif kind == "activate_stage":
            runtime.activate_stage(item["stage"], item["timestamp"])
        elif kind == "source_event":
            runtime.source_event_completed(item["event_id"], None, item["timestamp"])
        elif kind == "advance":
            runtime.advance(item["timestamp"], item["boundary"])
        elif kind == "delivery_started":
            runtime.delivery_started(item["event_id"], item["timestamp"])
        elif kind == "delivery_completed":
            runtime.delivery_completed(item["request_id"], item["timestamp"])
        elif kind == "ordinary_user_started":
            runtime.ordinary_user_started(item["timestamp"])
        elif kind == "agent_delivery":
            runtime.agent_delivery_seen(item["timestamp"])
        elif kind == "reply":
            runtime.add_reply(item["text"], item["start"], item["end"])
        elif kind == "played_audio_segment":
            runtime.add_played_audio(item["start"], item["end"])
        elif kind == "audio_diagnostic":
            runtime.add_audio_diagnostic(**item["diagnostic"])
        elif kind == "generated_text":
            runtime.add_generated_text(
                item["text"],
                item["start"],
                item["end"],
                response_id=item.get("response_id"),
                final=item.get("final", False),
            )
        elif kind == "prefetch_attribution":
            # Attribution is derived again by delivery_started from the
            # normalized operation journal; the original audit row is not an
            # input event and must not be applied a second time.
            continue
        else:
            raise ValueError("unknown persistent journal record")
    actual = runtime.summary(summary["closed_at"])
    for key in (
        "bindings",
        "transitions",
        "releases",
        "operations",
        "reply_windows",
        "audio_diagnostics",
        "confirmed_updates",
        "adapter_capabilities",
        "channel_status",
    ):
        if (
            key
            in {
                "audio_diagnostics",
                "confirmed_updates",
                "adapter_capabilities",
                "channel_status",
            }
            and key not in summary
        ):
            continue
        expected = summary[key]
        observed = actual[key]
        if key == "operations":
            # New attribution fields are absent from historical evidence. Keep
            # old replay deterministic without erasing them from new evidence.
            observed = [
                {
                    field: (
                        value.value
                        if field == "state" and hasattr(value, "value")
                        else value
                    )
                    for field, value in row.items()
                    if field in expected[index]
                }
                for index, row in enumerate(observed)
            ]
        elif key == "reply_windows":
            observed = {
                request_id: {
                    field: value
                    for field, value in window.items()
                    if field in expected.get(request_id, {})
                }
                for request_id, window in observed.items()
            }
        if observed != expected:
            raise ValueError(f"persistent replay differs: {key}")
    return actual

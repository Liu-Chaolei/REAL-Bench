"""Final evidence collection for a persistent-task run."""

from bisect import bisect_left


def finalize(orchestrator):
    """Collect final backend events and timed ASR including shutdown fragments."""
    from tau2.persistent_tasks.integration import advance

    coordinator = orchestrator.persistent_task_runtime
    runtime = orchestrator.environment.scenario_runtime
    advance(orchestrator, False)
    status = orchestrator.async_evidence_source.get_status()
    coordinator.record_channel_status(status)
    if status["evidence_gap"] or not status.get("ready", False):
        coordinator.issues.append(
            {"reason": "backend_evidence_gap_or_missing_capabilities"}
        )
    _, transcript_error = getattr(
        orchestrator.agent, "get_final_live_transcript", lambda: (None, None)
    )()
    if transcript_error:
        coordinator.add_audio_diagnostic("observer_transcript_error")
    audio_ms = orchestrator._scenario_audio_ms
    audio_times = orchestrator._scenario_audio_times

    def timestamp(milliseconds):
        if milliseconds is None or milliseconds < 0 or milliseconds > audio_ms[-1] + 1:
            raise ValueError("missing_or_invalid_played_audio_alignment")
        index = min(bisect_left(audio_ms, milliseconds), len(audio_ms) - 1)
        if index == 0:
            return runtime.timestamp_at(audio_times[0])
        fraction = (milliseconds - audio_ms[index - 1]) / (
            audio_ms[index] - audio_ms[index - 1]
        )
        return runtime.timestamp_at(
            audio_times[index - 1]
            + fraction * (audio_times[index] - audio_times[index - 1])
        )

    seen = set()
    for tick in orchestrator.ticks:
        if tick.agent_chunk is None:
            continue
        live = (tick.agent_chunk.raw_data or {}).get("live_transcript") or {}
        for begin_ms, end_ms in live.get("speech_intervals_ms", []):
            try:
                tick_offset = tick.tick_id * (tick.tick_duration_seconds or 0.2) * 1000
                coordinator.add_played_audio(
                    timestamp(tick_offset + begin_ms),
                    timestamp(tick_offset + end_ms),
                )
            except (TypeError, ValueError):
                coordinator.add_audio_diagnostic(
                    "invalid_played_audio_alignment", tick_id=tick.tick_id
                )
        if (
            live.get("origin") != "recorded_audio"
            or live.get("alignment") != "played_audio"
        ):
            continue
        for fragment in live.get("fragments", []):
            key = fragment.get("event_id") or str(fragment)
            if key in seen:
                continue
            seen.add(key)
            try:
                coordinator.add_reply(
                    fragment["text"],
                    timestamp(fragment.get("start_ms")),
                    timestamp(fragment.get("end_ms")),
                )
            except (KeyError, ValueError):
                coordinator.add_audio_diagnostic(
                    "invalid_reply_alignment", fragment_id=key
                )
    return coordinator.summary(runtime.time())

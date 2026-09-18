# V6: Passive Native Recovery Observation

V6 (`gaia2-qwen-audio/6`) changes only the evaluation adapter. The frozen qwen-audio-agent source, speech behavior, official task contracts, and judge configuration remain unchanged.

## Admission

Continue observing the exact local response-start watchdog only after the complete WAV and tail have been sent, a nonempty user transcript is confirmed, and no ASR, playback, speech delivery, or delivery acknowledgement is pending. The watchdog text is a runtime event, not proof that recovery succeeded or an external provider failed.

External billing, authentication, rate-limit and service failures still stop evaluation. Unknown errors and recovery outside this narrow scope also stop conservatively.

## Observation and timing

Let qwen reconnect through its existing native implementation. The adapter must not reconnect, replay input, redeliver speech, generate another response, or extend the task deadline. Recovery uses the remaining original task budget; the legacy 30-second recovery cutoff does not apply to this path.

A new connected/ready event must be followed by a fresh successful readiness query. Only then does the ordinary 15-second idle observation resume. Duplicate watchdog events clear stale readiness hints without restarting the budget. Pending recovery prevents idle completion.

## Evidence and scoring

Archive runtime events, connection progress, readiness probe timing, and `native-recovery.json`. Offline audit reproduces recovery accounting from protocol events. Report recovery count, confirmed recoveries, elapsed recovery time, and failure origin separately from the official task score.

V5 task-aware evidence rules remain in effect. A local watchdog alone neither forces a zero nor proves task completion. An aborted projection is `not_collected`; that is distinct from missing or corrupt archived evidence. No LLM classifies recovery or turn boundaries, and no manual labels are required.

## Validation and rollout

Use unit tests, network-disabled ARE integration tests, and the archived 0361 observer trace before live testing. Historical replay can establish only that observation would continue; it cannot establish native recovery success or a new task score.

Prepare a separate frozen five-task campaign (0361, 0340, 0498, 0094, 0084), preserving prior results. This implementation stage leaves it unstarted. Real recovery effectiveness and task accuracy require a subsequent live run.

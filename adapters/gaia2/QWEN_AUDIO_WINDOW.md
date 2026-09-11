# V4 fixed observation window

V4 (`gaia2-qwen-audio/4`) keeps free speech but removes online LLM boundary
classification. This is a new voice-adapted protocol, not original leaderboard
parity or a retrospective correction of v3 scores. It currently supports audio
only: exactly one source USER event, no ENV/CONDITION events, and at most one
oracle AUI answer. Unsupported scenarios are identified before service requests;
they remain in the original selection and are never replaced to improve scores.

## Observation and playback

After the full source WAV and 1.5-second tail, the microphone remains open with
zero PCM. Observation ends after 15 continuous seconds with no pending ASR,
frontend generation, playback, backend task, tool operation, notification,
connection recovery, or registered environment wait. The model's `completed`,
`waiting_user`, `final`, and `progress` declarations cannot close this window.
Heartbeat and status polling do not reset it. The total budget is the smaller of
the configured limit (at most 2400 seconds) and the scenario duration.

Admission closes before a final state recheck. Racing work reopens observation.
Playback receipts return immediately after confirmed playback; they do not wait
for official scoring, which avoids a backend/observer deadlock. Once observation
is frozen, further application calls are rejected. Provider logs are drained and
sealed before artifact hashing.

## Projection and scoring

All verifiably played text is concatenated in playback order with two newlines
between responses. Nothing is filtered by a model label, and no text is summarized
or replaced by the backend's requested speech. At most one AUI submission occurs
at observation close. Actual application events retain their original timestamps;
individual speech times and text offsets remain in `projection.json`. An empty
conversation does not create an artificial answer.

V4 uses ARE's offline-validation setup and official end-of-run judge. Official
semantic judging still uses the frozen configured judge; v4 does not claim that
this removes the official judge's own uncertainty. No separate semantic audit or
boundary LLM is called. Requested-vs-spoken differences are evidence, not an
additional task score.

A partially played response without verified heard-text alignment makes the
whole projection incomplete (`audible_transcript_incomplete`). Preserve the WAV,
generated text, and played sample count; publish no complete primary reward.
Never estimate a text prefix from an audio fraction, drop the interruption, add
ASR, or require human labels. Fully played provider transcript association is
checked, but audio/text semantic equivalence is explicitly not independently
verified.

## Metrics and audit

Report `input_audio_sec`, `first_application_action_sec`,
`last_audio_playback_end_sec`, `observation_end_reason`, `observation_end_sec`,
`elapsed_sec`, and `raw_wall_sec`. Last playback is not a verified final answer;
missing values stay null. Model declarations are informational only.

The scheduler audits each finished trial before admitting the next: exact file
inventory/hashes, audible projection, then offline official replay for normally
completed cases. Noncompleted outcomes receive integrity/projection checks, not a
claim of normal official replay. Audit failures and invalid measurements stop
admission; ordinary wrong answers do not. Resume revalidates stored evidence.
Results remain immutable; the ledger's `audit` and separate `.audit.json` hold
post-run audit status. `PROGRESS.json` and `PROGRESS.md` display all attempts and
the verified score denominator, not just successful/scorable examples.

The replay wrapper preserves a legal empty string parameter exactly while
retaining ARE's rejection of invalid None values. It never edits original exports.

## Prepare and inspect without launching

```bash
uv run python -m adapters.gaia2.evaluation_campaign prepare \
  --protocol gaia2-qwen-audio/4 --selection-limit 15 \
  --reuse-selection datasets/gaia2-campaign-v3-automatic-20260911-r9/manifest.json \
  --output datasets/gaia2-campaign-v4-window-20260911-pilot15

uv run python -m scripts.gaia2_window_shadow \
  --campaign datasets/gaia2-campaign-v3-automatic-20260911-r9 \
  --count 15 --output datasets/gaia2-v4-offline-validation/shadow15.json
```

Preparation does not launch jobs. The campaign must contain hash-linked
`observation-validation.json` evidence matching its frozen adapter. Keep the
current campaign stopped until live testing is explicitly resumed.

A later authorized launch uses `campaign_runner --stage audio --max-attempts 15
--services-restored`. The frozen `attempt_limit` is an upper bound even across
resumes. `--stop-after-current` lets the current case and its audit finish before
stopping; `--stop` archives and interrupts the active case. Remove an acknowledged
`stop-after-current` marker only as part of an explicit resume. Interrupted trials
require reconciliation and cannot be silently rerun.

Historical shadow projection never creates a new official score or extrapolates
unobserved activity after the old trace ended. Dynamic/multi-turn scenarios,
semantic speech-quality scoring, and qwen task-planning improvements are outside
this version; they require separately defined and validated protocols.

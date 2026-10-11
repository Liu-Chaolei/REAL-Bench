# GAIA2 Qwen audio evaluation protocol v3

The primary track evaluates the full Qwen system with its native freedom of expression. This is a **GAIA2 voice variant with standard technical adaptation**, not original GAIA2 leaderboard parity. Fixed progress scripts, forced faithful recitation, enlarged context windows and forced delegation are separate optimization experiments.

## Current rollout state

The v2 pilot was stopped by the user after 31 completed trials, one interrupted recording and 18 unstarted cases. Preserve it as diagnostic evidence. DashScope was reported unpaid from approximately 19:00 Beijing time on 2026-09-09 and has not been reported restored. No live trial or paid calibration should run until restoration is explicitly confirmed. Existing unit tests and mocked transports are not semantic-calibration evidence.

## Native behavior and input provenance

The speech frontend remains Ali `qwen-audio-3.0-realtime-plus`. Backend, title and judge use the configured Responses endpoint with `gpt-6-astra`, effort `low`. Credentials stay outside manifests and model-visible runtime state.

One original WAV is one benchmark request; VAD segments retain their native identities and map to that request. Every ASR segment is recorded. The evaluation coordinator restores the same recent-context and truncation policy used by ordinary Qwen, without extending its capacity. All background speech remains in the input. Do not inject the reference question, remove unwanted speech or automatically resubmit omitted tasks.

Backend results enter the existing frontend result-injection interface, using the native context length limit. Record the actual provider request payload and input acknowledgment as evidence. The frontend remains free to refuse, paraphrase, misunderstand or repeat. Score what was played; never substitute the backend's requested content. Application permissions, local notes/reminders and ARE apps retain their actual scopes.

## Independent turn assessment

Frontend `speech_kind` is no longer mandatory and `evaluation_delivery` is not added to the primary frontend tool list. Any legacy declarations remain hints. Accepted/running tool states do not classify a whole spoken response as progress.

A trusted, reference-blind assessor evaluates the actual transcript using only user/assistant conversation context visible to the system. Its API accepts no oracle, expected action or score. It makes two independent calls, records both requests and votes, and requires consistent valid turn-boundary decisions. Formal-subtype disagreements remain in the audit; the documented clarification/refusal/answer/checkpoint priority resolves their label. Boundary disagreement, uncertainty, malformed output or assessor unavailability produce `measurement_invalid`, not a model capability failure.

- Answers, substantive checkpoints, user-facing clarification questions and actual refusals become official AUI events.
- Pure progress does not advance an ARE turn. A nonterminal capability statement is retained for interaction review.
- Mixed speech containing a formal act is submitted once with its entire text. Refusals and answers cannot be hidden behind a progress label.
- Earlier formal answers are never erased. Native model-generated repetitions are retained; only duplicate transport submissions of the same delivery ID are deduplicated.

Official action, causality, timing and semantic checks remain unchanged. No task score is published for invalid measurements. Framework aggregate means are diagnostic; the specialized report separately counts scheduled, validly scored, failed, interrupted and unstarted cases.

## Playback, clock and lifecycle

A delivery tracks request/work/delivery/response/attempt identity and queued, generating, playing, played, committed, cancelled or failed status. Full playback plus final generation/transcript confirmation is required for commit. `evaluation_delivery_status` exposes trusted status. A model's completion declaration cannot manufacture a receipt.

Use `experimental.mcp_timeout = (scenario_timeout_sec + 600) * 1000`; backend wall-clock allowance includes the same grading reserve. Preserve the existing blocking delivery contract. Whole-request completion remains provisional until input is finished and tasks, queued responses, playback and waits have drained. A checkpoint does not close the request. Silent operations remain possible; the official validator determines whether a reply was required.

Classification runs alongside generation/playback where possible. Exclusive assessment pauses gate transport and new model/app dispatch, drain in-flight work, pause the ARE clock, and reject evidence if task activity advances during the excluded interval. If quiescence cannot be established, the measurement fails closed. Live validation must verify this against real provider behavior before any production claim.

Count genuine audio input, inference, operations, waits, playback and reconnection time. Never fast-forward waits. Record original audio duration, first successful application-call start, playback end, AUI commit, raw elapsed time, excluded pauses and reconnects. Received PCM and actually played PCM are separate files; partial output remains in the quality audit and never masquerades as a fully played response.

## Service recovery and shutdown

Recognize the actual `no user input was received for 180 seconds` inactivity closure. Pause transport during reconnection, keep the original request and clock, restore native conversation context, and retain pending delivery records. One explicit delivery transport retry may use the same ID with the next attempt number; do not repeat application mutations. Recovery has a 30-second outage budget and is measured separately from scoring pauses. A disconnect during an unfinished input recording invalidates the trial rather than silently continuing with an unconfirmed/truncated request; no synthetic text fallback is supplied. Backend-provider failures are recorded by the trusted proxy and are not treated as model capability failures.

All response creation waits for user speech to end. Environment notifications retain their original arrival time and identity, do not replace the user's turn, and do not force a backend task. A later notification can independently trigger model-selected work without being mistaken for duplicate submission of the initial request.

Billing/account failures stop the campaign; they are not model failures. A completed audio upload without any observable input processing or response is a service-input uncertainty, not evidence that the model chose silence.

The v3 launcher admits bounded windows. It does not admit another window after an invalid measurement or infrastructure failure. A user stop freezes scheduling, asks the runner to persist an aborted result, archives logs/audio, terminates the trial containers, and updates the job and campaign state. Original framework snapshots are retained. No stopped campaign resumes automatically.

## Separate interaction-quality report

`interaction-quality.json` includes every committed response, partial heard audio, backend-requested content, actual frontend inputs, dialogue-act evidence and human-review fields. Text differences are review candidates, not automatic failures: a paraphrase may be correct. Capability statements, internal placeholders, repetitions and content changes remain visible even when an utterance does not advance ARE.

Human review is optional. Automatic checks verify audio integrity and playback transport, but do not certify audio/transcript semantic equivalence. Quality findings never secretly alter the official task reward.

## Validation and commands

1. Run scoped Python/Node checks and the pinned ARE integration controls offline. Current mocked transport uses a real WAV and Gateway but no paid models.
2. Prepare at least 60 reference-blind, unlabeled utterances from preserved traces. No `expected_kind` or `audio_matches_transcript` fields are required. Run the frozen assessor twice per utterance; all boundaries must resolve and recorded calls must replay exactly. This is an automatic stability check, not an accuracy calibration: shared model errors remain possible. Audio hashes verify integrity only. Historical split names and `boundary-calibration` paths remain for compatibility; no held-out accuracy is claimed.
3. After confirmed restoration, run 13 fixed regression cases: 0680, 0059, 0797, 0215, 0732, 0777, 0084, 0651, 0036, 0436, 0597, 0010 and long-audio 0498. Task answers may fail; measurement defects must not pass.
4. Reuse only unchanged, complete first-attempt records and run the rest of the same frozen 50 audio cases at concurrency 1. Preserve interaction evidence for every case; human review is optional under the frozen `review_policy: automatic`. Require zero terminal infrastructure errors at this sample size and no unresolved measurement issues; there is no model pass-rate minimum.
5. Pass `audio-gate`, run the same 50 system-text and 50 backend-text controls, then validate load at 1/2/4. Stress-test jobs remain separate from primary scores. Full expansion requires the complete automatic gate, replay and model-profile evidence. Reports explicitly state that human semantic calibration and audio/transcript semantic verification were not performed. Existing campaigns retain their original review policy.

```bash
uv run python -m adapters.gaia2.evaluation_campaign prepare --output datasets/gaia2-campaign-v3 --reuse-selection datasets/gaia2-campaign-v2-20260909/manifest.json
uv run python -m adapters.gaia2.boundary_review --jobs datasets/gaia2-campaign-v2-20260909/jobs/gaia2-campaign-v2-20260909-audio --output datasets/gaia2-campaign-v3/boundary-examples.json
# No manual labels are needed; the following commands require confirmed service restoration.
uv run python -m adapters.gaia2.boundary_review --examples datasets/gaia2-campaign-v3/boundary-examples.json --output datasets/gaia2-campaign-v3/boundary-calibration --env-file /path/to/private.env --services-restored
uv run python -m adapters.gaia2.campaign_runner --campaign datasets/gaia2-campaign-v3 --stage regression --services-restored
uv run python -m adapters.gaia2.campaign_runner --campaign datasets/gaia2-campaign-v3 --stage audio --services-restored
uv run python -m adapters.gaia2.campaign_runner --campaign datasets/gaia2-campaign-v3 --stop
```

`--services-restored` is an explicit operator assertion, not an automatic check of the billing console. Actual service failures still stop the batch. The execution ledger allows first-attempt reuse only within the same frozen manifest. A previous invalid or interrupted run requires investigation rather than silent repetition; at most one separately recorded infrastructure retry may be authorized, and model mistakes are never retried to select a better score. `campaign_runner --campaign ... --prepare-infra-retry SOURCE_ID` prepares at most one separate retry configuration plus provenance for an existing infrastructure failure; it makes no model calls and does not overwrite the first attempt.


## Automatic preflight revision (2026-09-10)

Assessor `dialogue-boundary/3` distinguishes substantive partial answers from whole-request completion. `answer` plus `checkpoint` is a checkpoint unless a whole-request completion claim is present; formal content still cannot be progress. Both prompt and validator use the same precedence. Failed v2-prompt preflight evidence remains preserved.

`campaign_runner --stage smoke10 --services-restored` runs ten fixed diagnostic audio scenarios, two per category, serially and with the same preflight, freeze, and fail-fast gates. It reuses only first attempts in the same campaign. This diagnostic subset is not a representative capability estimate.

Assessor `dialogue-boundary/4` further distinguishes an access limitation plus explicit continued investigation from a task-level refusal. The former remains progress with an auditable capability claim; asking the user for access is clarification, and declaring inability to answer/execute is failure. This does not remove capability claims from interaction-quality evidence. The failed v3-prompt preflight remains preserved.

When boundary grading overlaps an already active speech response, the runtime releases its gates and waits with real time running. It records `assessment.concurrent_wait` and excludes no time. Active task work is not itself an invalid measurement; activity inside a claimed exclusive pause still is. A disconnected/unready gateway still fails closed.


## Long-input transport keepalive (2026-09-10)

The 665.2-second 0498 input exposed DashScope `response_idle_timeout` after 534 seconds with no generated reply. The interrupted attempt is invalid and retained. Qwen Audio connections now opt into an empty `session.update` every 60 seconds after initial configuration, clearing the timer on close. This changes no session fields, inserts no conversation content, requests no model response and does not change Smart Turn. Omni and other providers do not opt in.

An unscored raw-provider probe sent the complete original recording with 11 acknowledged empty updates, unchanged returned configuration, zero responses and no service error. This proves transport survival only; full Gateway input, model handling and scoring must still pass live checks. Keep the input-interruption and missing-upstream-confirmation guards. A fresh campaign retains all 50 selected source IDs and prioritizes 0498; earlier-version first attempts remain separate.

Protocol reference: https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events (`session.update` leaves omitted fields unchanged). The documented 300-second audio context retention window is not a maximum recording-duration rule and is not used to trim or exclude audio.


## Rejected input and empty response candidates

Smart Turn may emit an empty implicit response envelope while hearing a candidate utterance, then invalidate that utterance without emitting `response.done`. Retire only candidates associated with that invalidated input when there was no explicit response start, text/audio content, tool call, or programmatic waiter. Late empty envelopes do not reopen them; actual late output remains observable. This prevents phantom active responses from blocking completion.

A correlated provider transcription completion and `turn_invalid` event are positive evidence that the service processed and filtered that input segment. Record this separately (`input.filtered`) without adding discarded transcripts to the agent's conversation. After the whole recording is sent, if no request was accepted and no action or response occurs, classify the observed agent noncompletion as `protocol_failure` with cause `agent_input_filtered_no_response`, not as missing ASR/service confirmation. This is a failed system outcome, never a passing task score or a claim of human-verified audio semantics. Missing confirmation remains an infrastructure error; any disconnect during unfinished input remains invalid. All unrelated delivery and timing guards remain enforced.


## Response creation racing user speech

A new speech start may cancel a locally queued response before the provider rejects that creation request. Recognize the late `input_busy` refusal only when exactly one transmitted, unstarted request was cancelled by that input item within two seconds, the error explicitly identifies `response.create`, and no other response is pending or active. Record `evaluation.response_rejection_handled` with `cancelled_before_start`; do not reissue a cancelled utterance. All raw provider errors remain in the audit log.

For an uncancelled creation explicitly rejected before any response start/output, retry only the identical response payload after input/response idleness and dispatch clearance, with at most three transport retries. Never repeat conversation-item insertion or tool execution. Accepted/ambiguous requests and unproven errors still fail closed. This is transport error reconciliation, not retrying task answers to improve scores.


## ASR failures and reviewed diagnostic continuation

A provider `conversation.item.input_audio_transcription.failed` is a service failure, even after an earlier accepted segment. Release only the matching input state, report the error, and invalidate the measurement immediately. Preserve the raw cause (`provider_asr_failure`, or billing/authentication when explicitly indicated). Do not leave the UI listening indefinitely or score the incomplete input as a model mistake.

For a diagnosed ASR service failure, `--prepare-infra-retry SOURCE_ID` prepares at most one independent trial in `retries/`, never in the primary score directory. Run that configuration, then `--accept-infra-retry SOURCE_ID` verifies the original failure, frozen configuration, audio, model/source identity and a measurement-valid retry. A failed model answer can still prove service recovery. Acceptance permits resuming diagnostic admission for the unrun cases only. Original infrastructure failures remain invalid in the primary ledger, denominator and expansion gates; no retry answer replaces a first attempt. Configuration/result changes invalidate the review. Other measurement failures and uninvestigated service failures still stop admission.


After the recorded waveform and its existing 1.5-second tail have been sent, the audio driver continues streaming zero PCM at real time until the trial ends, as a live duplex microphone would. This adds no speech and changes no original WAV samples; the reported input duration remains the original recording. Quiet frames do not reset the idle deadline and pause with transport during exclusive grading. The old fixed EOF cutoff could leave an unfinished VAD/ASR turn without enough end silence. This is a possible contributor to the observed ASR timeout, not proof that all 504s are client-caused.

For providers advertising ASR, pending input items must settle before request completion, an exclusive grading pause, or a reconnect may treat input as confirmed. ASR failures still invalidate the trial; continuous EOF silence is not used to conceal them. Record `input.completed`, filtered input evidence and ASR failure details for audit.

### Backend evidence finalization

Before writing artifact hashes, the runner permanently seals backend proxy admission
and waits for admitted requests to finish writing their response metadata. Queued and
late requests receive HTTP 409 without modifying trial evidence, including requests
with invalid model profiles. This prevents a retry after a provider failure from
changing `provider-failures.jsonl` after hashing. A failed seal invalidates measurement.
Finalization is outside the recorded task execution interval. Historical hash
mismatches remain audit failures; do not regenerate their integrity manifests.

## V4 fixed observation window

The opt-in `gaia2-qwen-audio/4` protocol uses a deterministic idle window and one
lossless audible projection instead of the v3 dialogue-boundary model. V3 evidence
is retained separately. See [QWEN_AUDIO_WINDOW.md](QWEN_AUDIO_WINDOW.md) for scope,
partial-playback policy, audit gates, metrics, and preparation commands.

## V5 task-aware evidence

`gaia2-qwen-audio/5` derives scoring requirements from the official ARE reference
filter. Normal barge-in no longer automatically blocks task grading or the whole
batch: action-only tasks are scored; answer evidence gaps remain unknown and
continue. See [QWEN_AUDIO_TASK_EVIDENCE.md](QWEN_AUDIO_TASK_EVIDENCE.md).
 
## V6 passive native recovery

See [native recovery protocol](QWEN_AUDIO_NATIVE_RECOVERY.md) for the narrow observation scope, readiness evidence, original-budget timing, and validation requirements.

## V7 official necessary-action decisions

See [V7 decision policy](QWEN_AUDIO_ACTION_DECISION.md) for deterministic failure certificates, chronological playback evidence, and score-basis reporting.

## V8 response-bound interruption evidence

See [V8 interruption evidence](QWEN_AUDIO_INTERRUPTION_EVIDENCE.md) for identity-based cancellation association and independent audit. Playback behavior remains unchanged.

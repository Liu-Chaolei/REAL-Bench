# REAL-derived metrics validation

Implementation date: 2026-11-06. This record distinguishes implementation checks,
reference verification and real model trials. No complete real-model benchmark
score is claimed.

The 2026-11-07 intent v2 implementation, live duplicate-delegation examples and
saved-call regrading are recorded separately in
[duplicate delegation validation](intent-repetition-validation.md).

## Verified behavior

- The REAL source baseline is commit `7e9224a80cb6a5abb5b457dd89988859c8b97234`.
  `SOURCE.json` records original file hashes and ARE 1.2.0 source provenance.
- All 800 current Gaia2 tasks pass the business-tool/parameter/effect-handler
  inventory: 28 business WRITE tool types, no unsupported referenced fields.
- All 800 pass native reference execution and strict replay with diagnostic
  state capture; there are zero capture errors and zero diagnostic exceptions.
  These key-free checks make no model calls. Semantic checks and equal virtual
  timestamps can remain unknown.
- All 800 also pass the existing Gaia2 verification/seal command. Runtime digest:
  `02dce82ece6bd749593b51fe0ee1763f0dcd32366bfb745ac384596f0557af9e`.
  The existing verifier separately records five authoring assertion failures;
  these are not hidden or manually waived by the diagnostic suite.
- The diagnostic suite contains 160 tests covering REAL scoring parity,
  whole-call inputs, unreleased requests, independent prediction, empty backend
  submission, partial delivery, application identity, receipt binding, equal
  timestamps, real waveform analysis, state capture and original grading errors.
- The combined diagnostic/runner/checkpoint/results regression passed 339 tests
  before the last additional default-parameter test; the final diagnostic-only
  result is recorded in the current test log.
- `make test-voice`: 708 passed, 3 skipped, 138 deselected. The provider suite
  retained its explicit live gates: 132 skipped, no failures/errors.
- The complete updated Qwen image passed 15 offline container tests, including
  real stock Qwen/ACP/MCP execution with mock provider services and backend
  evidence enabled. The standalone bridge unit suite passed eight tests.

Local artifacts are under `data/simulations/real-metrics-validation/`:
`coverage-final.json`, `reference-final/report.json`,
`reference-corrections/report.json`, `gaia2-seal-accepted.log`,
`test-voice-final.log`, `acceptance-final.log`, image build logs and model probes.

## Source-data and evidence limits

Reference execution is not a guarantee of a perfect diagnostic score. Equal
virtual operation times cannot prove strict precedence; native semantic checks
are deliberately not called in key-free verification.

The later task continuations include Files.open calls with `data` or `content`
inside kwargs. The native method opens/truncates a file and does not itself write
those supplied strings. The new effect validator compares actual bytes with the
specified content, exposing these reference effects as failed where applicable.
It does not credit a nonempty file solely because the API call returned success,
and does not modify the task data to hide the difference.

Dynamic external conversation-membership updates cannot be validated against a
static initial membership set. That predicate remains unknown when the necessary
causal membership evidence is not available. Other tool, parameter and state
checks remain reportable. READ-return references are resolved only from matching
successful observed calls preceding a consumer, with agreement across candidates.

## Real service validation

The independent configured diagnostic judge (`openai/responses/MiniMax-M3`)
passed a real connection probe. A fixed two-request intent interface fixture
completed extraction and both checking directions in five model requests,
producing F1=1. This is an interface fixture, not an Agent ability score or human
verification of judge semantics.

A ten-task sample was frozen before observing model outcomes: two per category,
including pure query, writes, intermediate state and dynamic events. The first
attempt failed during the MiMo speech frontend startup before user conversation.
The pilot stopped admission and preserved the infrastructure-error record;
the other nine tasks were not run. A separate frontend warmup HTTP probe returned
401. Neither the failed startup nor unrun tasks are treated as model failures.

Pilot records: `data/simulations/real-metrics-live-20261106/report.json`.
Service evidence: `real-metrics-validation/frontend-health.json`.
The real ten-task semantic/audio acceptance remains incomplete until that
frontend configuration can successfully authenticate. No automatic retry or
model substitution was performed.

## Capture overhead

A deterministic native-tool comparison with collection off/on produced identical
outputs and final application state, six calls in each condition, and no model
calls. On this host the sample mean was 0.0652 seconds without collection and
0.1158 seconds with collection. Captured frames occupied approximately 6.4 MB.
These are a small local measurement, not a general performance guarantee.

## Repository-wide checks

`make check-all` was run and failed on the pre-existing
`scripts/manual_fix_stage2_0074.py` lint issues. A separate format check also found
pre-existing formatting in `src/tau2/task_authoring/gaia2_series/manual.py`.
Excluding those two files, repository lint and formatting pass. `git diff --check`
passes. These unrelated scripts were not rewritten.

The full core run finished with 3264 passed, 57 failed, 28 skipped and six setup
errors. One failure was an early version of the new waveform fixture; it was
corrected and the final diagnostic suite passes. The other reported failures
concern frozen multilingual/retail/intake bindings, inventories and prompt
snapshots; no published packs or benchmark pins were regenerated to conceal
those differences. The complete core suite is therefore not reported as passing.

## DeepSeek frontend follow-up

The first selected task was retried in a new run directory using only the
frontend LLM references `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, and
`DEEPSEEK_MODEL` from the existing private configuration. The ASR, TTS, backend,
caller and judges kept their existing profiles.

The frontend reached ready, produced 31 audio chunks, delivered a confirmed
backend prompt, and issued one `Files__tree` tool call. The run subsequently
failed on the simulated user's existing `openai/mimo-v2.6-pro` profile with
`RELAY_102` (credential disabled or expired). This establishes frontend startup,
audio and delegation connectivity, not a completed task or complete metrics.

Configuration: `integrations/qwen_audio_agent/config.gaia2.metrics.deepseek.json`.
Evidence: `data/simulations/real-metrics-deepseek-frontend-20261106/frontend-test-report.json`.

## DeepSeek frontend and simulated-user follow-up

A further independent attempt set the simulated user's key, base URL and model
references to the same `DEEPSEEK_*` settings, retaining the prior DeepSeek
frontend and the existing ASR/TTS/backend/judge profiles. Both frontend and
simulated-user model calls succeeded. The configured caller resolved to
`openai/deepseek-flash`.

The conversation ran for approximately 228 seconds and made 37 application tool
calls. It stopped in the first request when the user's grounding model returned
`issue_kind="none"`, outside the declared enum, twice (initial response and format
repair). The result is `user_error`, not a valid task-completion score. Separately,
the intent diagnostics requested the default `gpt-5.5`, which the configured
endpoint rejected; no intent F1 is available. Waveform metrics were produced as
diagnostic-only measurements for the invalid trial.

Configuration: `integrations/qwen_audio_agent/config.gaia2.metrics.deepseek-user.json`.
Report: `data/simulations/real-metrics-deepseek-frontend-user-20261106/deepseek-user-test-report.json`.
No simulator validation rule or judge profile was changed for this attempt.

## DeepSeek complete intent re-grade

The completed three-stage run was re-graded from saved evidence with all three
intent roles (`gt`, `prediction`, and `evaluation`) using the frontend profile
`openai/responses/deepseek-flash`, the same `DEEPSEEK_BASE_URL`, and the named
`DEEPSEEK_API_KEY`. No audio, Agent, user simulator, or business operation was
replayed. The whole-call result was Precision **0.8730**, Recall **1.0000**,
and F1 **0.9322**. The task itself retained its original reward `0` because
the second-stage Dublin contact answer failed the task requirements.

The score artifact is under the `b5f03f34c4e544d2ab99cca11211c29c46bf8f98885a836970816570e081002d`
profile directory in the saved run.

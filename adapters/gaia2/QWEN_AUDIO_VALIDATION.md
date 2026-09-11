> 2026-09-10 workflow update: new campaigns use automatic review. Boundary examples no longer require `expected_kind` or `audio_matches_transcript`; the preflight checks two-call boundary stability and exact evidence replay, not human-labeled accuracy. Human pilot review is optional. Historical calibration/audit requirements below describe earlier candidates and are superseded for new automatic-policy campaigns. No live results are implied by this change.

# Qwen audio integration validation

## Protocol v3 — offline implementation

The primary frontend retains free expression. Mandatory frontend classification
and automatic accepted/running-to-progress projection have been removed. Actual
spoken output is assessed twice by a reference-blind evaluator; disagreement or
unsafe timing pauses invalidate measurement rather than penalizing model ability.
Backend requests, actual provider inputs, played audio and user-facing results
remain separate evidence. Native recent-context and result-length limits are retained.

The v3 launcher requires explicit confirmation of service restoration and a
passing, human-labeled boundary calibration before live trials. It stops further
admission after measurement/service faults, preserves model failures without
selective retries, and archives a user-aborted trial with explicit stopped state.

The prior pilot is stopped. DashScope restoration has not been confirmed. The
60-utterance review packet has no fabricated human labels; real assessor calibration,
13-case live regression, the new 50-case audio pilot, text controls and live load
validation remain gated. No v3 model score or production readiness is claimed.
Executed offline evidence is recorded separately under `jobs/gaia2-protocol-v3-checks/`.


## Protocol v2 implementation — 2026-09-09

The older results below are **prototype debugging evidence**, not trustworthy
full-system scores. In addition to progress/turn-boundary errors, v2 found that
lowercasing `[[True]]` made ARE's case-sensitive sanity checker reject valid
responses. The Responses transport now preserves verdict text and rejects
unparseable verdicts as infrastructure errors.

Implemented: explicit delivery/request lifecycle, playback-confirmed AUI
projection, checkpoint barriers, real-time notification waits, backend speech
proxy, three diagnostic tracks, root-only control credentials, non-root backend
with ARE-only tools, credential-owning Responses proxy, request/response and
judge replay artifacts, frozen stratified selection, audit and expansion gates.

Executed evidence is under `jobs/gaia2-protocol-v2-checks/`:

- 30 known-label cases × 3 repetitions passed the official sanity and
  user-message checker chain: **90/90**, 180 fresh judge calls. This is a small
  synthetic calibration, not human validation of benchmark semantics.
- Official ARE integration: 5 tests passed, including real waiting, checkpoint
  turn synchronization, valid/missing/failed action controls, and verdict parsing.
- Real WAV → real Gateway → mocked speech provider passed without text fallback.
- Docker backend identity test passed: non-root cannot read control files, root
  process environment or the reference scenario. Live backend requests exposed
  ARE tools only and used Responses, `gpt-6-astra`, effort `low`, including title calls.
- Recorded audio trace replay reproduced AUI mapping, score and three judge
  calls offline, with zero fresh inference calls.
- Real search debugging runs have achieved official verifier success in audio,
  system-text and backend-text tracks. Other debugging attempts exposed provider
  timeouts, repeated frontend responses and lifecycle issues; all are retained.
  They are not a 50-case validation set and must not be pooled into a score.

**Production gate remains closed** until the final protocol passes the frozen
150-run pilot, concurrency 1/2/4 checks, complete trace checks and the agreed
20-case human audit plus anomalies. No 633-case production score is claimed.
Global Ruff/type checks have pre-existing failures (424 global ty diagnostics);
use the separately recorded scoped checks to assess these changes.


Validation date: 2026-09-08. Changes span this repository and the adjacent
`qwen-audio-agent` checkout. The initial offline checks below made no paid model
calls; later live runs and the provider change are recorded at the end.

## Passed

- Validated 633 existing WAVs against local GAIA2 IDs: execution 88, search 137,
  adaptability 151, time 142, ambiguity 115. All are mono, 16 kHz PCM16; no empty,
  truncated or unmatched files. This checks format/identity, not spoken semantics.
- Generated five fixed smoke tasks in `datasets/gaia2-qwen-audio-smoke`.
  Harbor's actual dataset loader discovers all five, and task schemas validate.
- Built both runtime images and the generated two-service Docker Compose project.
  The local Compose version successfully used its classic-builder fallback.
- Harbor's 12 focused audio tests passed, including audio integrity, isolation,
  playback receipts, cancellation, completion, credential separation, and handling
  empty Docker stdout/stderr. Ruff passed for the newly added Python modules and
  tests; explicit type checking passed for the five host-side integration modules.
- Qwen's server suite passed all 442 tests, and its repository lint passed.
- Pinned ARE/MCP integration checks passed inside Docker without network access:
  full scenario tool schemas build; AUI text getters are absent; failed app calls
  preserve official exception events; the official judge accepts the oracle trace
  and rejects missing or failed agent actions. Semantic judge responses were mocked.
- Used the supplied `search/0251_ra2mla9wv2ggujnc6130gzqwp1f6agmm.wav` through the
  real Qwen Gateway with a local mocked speech provider. The provider verified the
  received PCM hash; output audio, transcription and playback receipts completed.
  The backend was disabled for this transport test, so it is not a model score.
- Separately started the real installed OpenCode backend and Qwen Gateway inside
  a network-disabled container; health reported `gateway_ok=true`, `protocol=opencode`.
  This did not exercise model inference.

## Whole-repository checks are not clean

The requested full Harbor unit-suite invocation completed with 4,988 passed,
160 failed, 53 skipped and 77 errors in the prepared Python 3.12 environment.
Failures include unrelated provider, preflight and adapter-wizard tests. Whole-tree
Ruff and `ty check` also failed; the final full type check reported 424 diagnostics
in the configured existing source scope. Formatter/linter edits outside this task
were reverted. These results must not be represented as a clean full-suite run.

Session logs are available locally:

- `/tmp/gaia2-targeted-tests.log`, `/tmp/gaia2-targeted-ty.log`
- `/tmp/gaia2-qwen-server-tests.log`
- `/tmp/gaia2-official-docker-test.log`
- `/tmp/gaia2-mock-smoke.log`, `/tmp/gaia2-mock-audio-smoke/`
- `/tmp/gaia2-unit-suite.log`, `/tmp/gaia2-ruff-check.log`, `/tmp/gaia2-ty.log`
- `/tmp/gaia2-compose-build.log`, `/tmp/gaia2-opencode-startup.log`

## Live verification and provider change

DashScope speech, backend and judge connectivity checks succeeded with the supplied
private credential. Five unique scenarios were attempted: search, execution, time
and adaptability received official failure decisions; ambiguity stopped on a
normal user-interruption cancellation that the driver incorrectly treated as fatal.
That handling is now fixed, along with a pending-playback counter that included
function-only responses. The regression suite now has 15 passing focused tests;
Qwen's server suite has 444 passing tests.

The full traces contain successful backend actions even when the official online
judge cached a failure on an earlier progress reply. These scores must not be
interpreted as a final-state task completion rate. Search's separate verifier was
replayed successfully after installing Buildx, without repeating inference.

The user requested no further Ali API calls. Live execution is paused; the job now
selects `~/.config/qwaudio/gaia2-evaluation-non-ali.env`. The wrapper supports the
existing self-hosted speech-to-speech frontend, explicit OpenCode provider/model
names, custom backend configuration and an independently configured judge.
Replacement service URLs and credentials are still needed. No non-Ali service has
yet been connected or benchmarked. See `jobs/gaia2-qwen-audio-live-20260908-summary.json`
for the preserved live-run results and source paths.


## Current profile: Ali speech + GPT-6 Light

The user subsequently selected Ali for speech, with GPT-6 for both backend and
judge. The supplied Codex auth file contains an API key and its config selects a
Responses service. That service rejects `gpt-6` and lists `gpt-6-astra`; the latter
was verified successfully. The UI label Light maps to API effort `low`.

The private active profile is `~/.config/qwaudio/gaia2-evaluation-ali-speech-gpt.env`.
Captured native OpenCode requests confirmed `/v1/responses`, `gpt-6-astra` and `low`
for both primary and auxiliary model calls. The new Responses judge transport also
passed a real request and preserves ARE's existing checker contract. Focused
Python tests now pass 17 cases; targeted lint and type checking pass. Whole-tree
checks retain the previously documented unrelated failures.

The five-scenario live smoke completed with zero runtime errors and zero official
passes. Some frontend responses did not delegate; other failures were cached by
the online judge before later successful actions or answers. The result is not a
standalone GPT capability score. See
`jobs/gaia2-qwen-audio-gpt6-light-20260908-225230/SUMMARY.md` and
`jobs/gaia2-gpt6-backend-request-metadata.jsonl` for evidence without credentials.

# Gaia2 migration validation — 2026-10-25

The native `gaia2` domain is implemented and its 800 tasks are sealed. It uses
the shared τ run, user, voice, scenario and evaluation interfaces. Live model
acceptance is currently limited by the configured MiniMax Token Plan API.

| Check | Result |
|---|---|
| Imported core tasks | 800; 160 in each of five categories; 24 snapshots |
| Reference execution, business state and strict replay | 800/800 passed |
| Required resources | 298 pinned files; no missing resources |
| Gaia2, scenario, replay, scoring and provider capability tests | 42 passed |
| Voice and streaming regression | 616 passed, 3 skipped |
| Core targeted regression | 118 passed |
| Full core regression before compatibility fixes | 2,906 passed, 28 skipped, 25 failed; 9 compatibility failures were subsequently fixed and checked by targeted regression |
| Remaining full-core failure group | 16 existing τ-Entity released-binding mismatches; see below |
| Parallel complete image contracts | 13/13 passed in each of two simultaneous containers |
| Five simultaneous containers | Distinct ports and tokens; closing one left the other four running |
| Five concurrent application trials | State, RNG and scene-clock results matched isolated serial executions |
| User grounding calibration | 3 positive and 3 negative cases passed using the configured real judge |
| Saved-result ENV regrading | Search and execution reference fixtures passed through the standard entry point; the execution fixture replayed 41 tool records |
| Ruff lint and formatting | Passed |
| Live retail regression, task `0` | Normal user stop; reward `1.0` |
| Live Gaia2 acceptance | 10 attempts, 10 infrastructure errors from MiniMax `2062`; no scorable model outcomes |

The saved-result fixtures are explicitly marked as reference fixtures and make
no model calls. They verify environment scoring and replay; they do not measure
agent accuracy.

## Frozen inputs

The final runtime digest is
`83f7b6532c63a6e06ebd6d18de67772c43a6e1cf3850e2e348444f20bac5ec5e`.
The sealed task digest is
`d1de6c65f94f92d3fec4bc050c1c56b95fc0a9addf31fd8b08f7f7af76ff9cbc`.
Resource identities and upstream provenance are recorded with the dataset.

The tested Qwen image is
`docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.0`, image ID
`sha256:d2b1fbe7900b9ce7855dc796f3a03840d72dd8c1b8181a79fe2fdd85425eb0d2`.
The speech frontend uses Hugging Face Speech-to-Speech 1.1.0, MiMo ASR,
MiniMax-M3 through Responses API and MiMo TTS. The native Qwen Code backend
retains its existing configuration references.

## Live acceptance and rate limit

Selection uses `random.Random(300)`, sorted categories and source IDs, with two
tasks sampled without replacement per category. The final run uses five
concurrent calls, a 1,200-second conversation budget, a 1,350-second wall-clock
timeout and zero automatic retries. The user explicitly selected the 20-minute
budget and requested increased concurrency. Task-specific timing requirements
remain separate from this run budget.

All ten attempts failed in the user-simulation API call path with
`rate_limit_exceeded`, provider code `2062` (Token Plan rate limit). Every failed
trial records exactly one attempt. Infrastructure failures are excluded from
agent accuracy; the eligible sample size is zero and accuracy is unavailable.
These results must not be reported as a zero-percent agent success rate.

Continuing live acceptance requires an API profile with sufficient quota or an
explicit rerun at lower concurrency. The existing private dotenv configuration
was not edited. No account upgrade or billing change was performed.

## Development probes and baseline failures

Earlier development runs exposed false grounding rejections of ordinary status
questions and conditional approval. Grounding now checks unsupported personal
facts/preferences and material changes to the business objective. Procedural
consistency and task completion are handled by the conversation and ordinary
task evaluators. The six-case calibration preserves rejection of changed dates,
quantities and invented preferences.

Three terminal user-simulator errors and two interrupted development trials are
retained separately from final acceptance. Their logs, audio and archived runtime
and data versions remain available. No unsuccessful trial was overwritten or
silently retried.

The 16 remaining core failures belong to the existing `es/intake` released-binding
mismatch. Reconstructing the initial dirty workspace, before the Gaia2 additions,
already gives binding
`70e6323024ab706294cfc33026de3d7ca735619a50011e89d9eff8305a1fb4e0`, whereas
the frozen frame expects
`b94cb5153c5a4ed9469a48b66b16d21a624d08ecdb5fee7b4360c0c88d053a93`.
Released language data and benchmark pins were preserved.

## Local evidence

Evidence is stored locally in
`data/simulations/gaia2-migration-validation-20261025/` and is not committed:

- `validation-report.json`: complete status and links to evidence.
- `offline-verification.json`, `selection-acceptance.json`: final pins and sample.
- `gaia2-acceptance/results.json`, `rate-limit-diagnosis.json`: all ten outcomes.
- `retail/results.json`: successful retail regression.
- `five-container-isolation.json`, `parallel-image-contracts.json`: concurrency.
- `reference-regrading*.json`: key-free saved-result evaluation checks.
- `core-regression.json`, `baseline-pin-check.json`: core regression diagnosis.
- `gaia2/`, `gaia2-final/` and the two runtime archives: development attempts.
- `secret-scan.json`: credential scan of generated evidence.

The [Gaia2 guide](gaia2.md) contains installation, import, run and scoring details.

## Reliability updates after the five-call audit

NL checks now use assertion IDs rather than byte-for-byte copies of criteria.
Legacy single-result judges remain readable; legacy multiple-result output uses
unique whitespace-normalized correspondence. Original criteria and reward
composition are retained. Missing, duplicate and non-boolean results still fail.

Business-state matching aligns original fixed IDs before searching generated
identities, preserving foreign-key bijections and checks of untouched objects.
Only the remaining generated subset uses unordered matching. The search budget
remains a grading-failure boundary, never a negative business-state judgment.

Scenario callers privately regenerate rejected free text at most twice. The MiMo
observer provides bounded, on-demand audio review for decision-changing time,
price, quantity and negation conflicts. Both primary ASR and supplementary review
remain auditable; neither reference answers nor unplayed captions enter the caller.

Raw trajectories are checkpointed before scoring. New optional evaluation status
and error metadata preserve completed conversations when judges fail, and those
runs are excluded from agent metrics. Regrading writes separate results.
These shared runtime edits require complete reference/replay verification and
resealing; task goals, source assets and old online results are not rewritten.

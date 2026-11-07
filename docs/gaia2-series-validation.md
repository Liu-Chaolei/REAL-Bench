# Gaia2 series authoring validation — 2026-10-27

The offline authoring pipeline is implemented. Runtime delivery of staged requests
and live stage-aware grading are deliberately outside this change. The original
800 tasks, their snapshots/resources, and released benchmark pins were preserved.

## Verification

### Full-run launch — 2026-10-28

The clean full dataset is `data/tau2/task_series/gaia2/v1-full`. Preparation
imported exactly the five approved pilot records and 24 material indexes; the
remaining 795 task records begin unstarted. Explicit content-bound approvals are
recorded in the pilot's `pilot-human-decisions.jsonl` and the full migration log.

The full runtime snapshot is
`data/simulations/gaia2-series-full-20261028/runtime/src`. Preflight confirmed the
same sealed runtime/resource identity, matching model configuration, no stale
pilot approvals, and zero model calls during dry-run. The background runner starts
at eight workers and may increase to sixteen only after the configured health
and warmup checks. It stops before human anomaly approval and publication.

Validation: 83 targeted tests passed, including eight- and sixteen-worker native
environment isolation, full migration, provider recovery, repeat-run resume and
human gating. Ruff lint/format and launcher shell syntax checks passed. On launch,
eight new tasks entered generation and the first sixteen recorded model requests
returned successfully without provider errors. MiniMax's missing LiteLLM price
mapping produces cost-accounting log messages; these are distinct from API failure.

Progress is in `progress.json`, `full-run.json`, per-batch reports and `full.log`.

| Check | Result |
|---|---|
| Authoring, native Gaia2, scenario runtime/replay/metrics/assertion tests | 64 passed |
| CLI and multilingual voice-construction checks with the HPC native library path | 26 passed |
| Ruff lint | Passed |
| Ruff format check | Passed |
| Original task population | 800 tasks, 24 indexed snapshots |
| Material-backed example series | Five series, one per original category; all reference-executed and strictly replayed |
| Independent model review of those examples | Five accepted; zero human signatures |
| Live model-authored series | One two-continuation variant, reference/replay verified and independently accepted |

The original runtime pin remains:

```text
aba643fdd586918ebc30796dee23959f9a56bef9706b8dfea1fece4ed101e919
```

The examples and the model-authored variant share one original search task, so
these are six candidate sequences over five unique originals, not six originals.
They are review previews, not a complete 800-series release.

## Reviewable artifacts

Local artifacts are under `data/tau2/task_series/gaia2/`:

- `v1-examples/requests.md`: the original and two continuations for five examples.
- `v1-examples/review.md`: complete evidence, checks and independent review decisions.
- `v1-examples/preview.tasks.json`, `preview.tasks-2.json`, `preview.stages.jsonl`:
  native task previews, exact two-stage prefixes and stage/evaluation records.
- `v1/requests.md`, `v1/review.md`: the live model-authored variant and its audit.
- `v1-initial-attempt/`: preserved initial provider-timeout diagnostics.

Generated work records, indexes and journals are local, rebuildable artifacts.
Human templates remain unsigned. Full export requires all 800 series, the fixed
50-series pilot and additional 75-series human sample, plus flagged duplicate goals.
`--allow-partial` only creates an explicitly incomplete draft export.

## Issues found and addressed

Real generation exposed repeated material browsing, prose instead of structured
results, and numeric differences between raw state and public tool serialization.
The author now has bounded targeted retrieval, a final tools-disabled JSON turn,
bounded JSON correction, exact material pointers with public-read proofs, and
checks that mirror the existing serialization boundary. Speech checks use actual
reference delivery evidence while preserving native null speech-anchor results.
Deterministic projected state equality avoids unnecessary identity backtracking;
the original entity matcher remains the fallback.

Every API call's usage/error is persisted as it occurs. Provider failures stop new
calls in that batch and do not consume a content-quality attempt. Rejected reviews
can trigger regeneration within the existing attempt budget. Content or authoring
implementation changes invalidate stale verification/review records.

## Broader suite limitations

The full core run was interrupted after 2,277 passed, 12 failed and six setup
errors. The observed failures included the already documented frozen `es/intake`
binding mismatch and native audio imports missing `libasound.so.2`. The latter also
caused CLI help to contain an optional-dependency diagnostic. Supplying
`.venv/lib/tau-native` through `LD_LIBRARY_PATH` passed the 26 focused CLI and
voice-construction checks. The pilot driver supplies this path automatically.
Released language data and frozen benchmark pins were not changed to conceal the
binding mismatch. The complete core suite is not reported as passing.


## Retry and isolated runtime — 2026-10-27

The retry uses one worker, a 600-second per-request timeout and at most two
transient API attempts. Exhausted provider failures are isolated per task by
default; whole-batch cancellation is opt-in. The operational overrides preserve
previously authored content and the generation configuration identity.

Two previously interrupted pilot tasks (0002 and 0082) now have complete
three-stage sequences, strict reference/replay verification and independent model
acceptance. Across their first retry/review runs, 43 model requests completed with
zero API errors; the longest recorded request took approximately 172 seconds.
Task 0002 required aligning two citation/check pointers from `/messages/2/content`
to `/messages/7/content`: the exact same source text was found uniquely in the
public response. Requests, expected facts, answers and quality-attempt budgets
were preserved. Task 0082's generated entity arguments were normalized to original
creation-event references without changing their resolved values or business goals.

Concurrent changes to the workspace's Gaia2/ASR code then invalidated the original
runtime binding. To preserve both that work and the original sealed data, a stable
code copy and independent data copy were created under
`data/simulations/gaia2-series-isolated-retry-20261027/`. The copied source passed
all 800 reference executions and strict replays before being sealed independently.
Its runtime hash is:

```text
eeb5257eb044a4a5b3ed98f721dba7d15608b4b6a6a8b0e5bea2ab0a29913aba
```

The three complete authored sequences were migrated to
`data/tau2/task_series/gaia2/v1-isolated-retry/`, reverified and independently
accepted again. Migration checked that base task content changed only in its
runtime pin; original runtime/configuration provenance is retained in work
history. The isolated runtime also passes all 68 targeted tests.

`data/tau2/task_series/gaia2/active.json` identifies the active dataset and immutable
runtime. The 50-task pilot resumes there with the serial retry controls. Human
review remains unsigned and no complete 800-series release is claimed.
# Repair and five-task pilot update (2026-10-28)

The active pilot is `data/tau2/task_series/gaia2/v1-repair` and contains the same
five selected tasks: 0356, 0435, 0507, 0601 and 0956. All five have two
continuations, successful reference execution and strict replay, and accepted
independent model reviews. No human approvals have been imported and no full run
or release has been started. `requests.md` is the concise reading sheet;
`review.md` and five unsigned JSONL templates contain the complete review material.

Task 0356's first continuation was repaired by the model on its first additional
attempt, correcting the order-details evidence pointer. Its second continuation
was generated and verified. Task 0601's previous last candidate passed without
another author-model call after evidence reads were moved to an isolated
checkpoint environment. The main journal no longer contains evidence queries.

Validation: 73 tests passed across task authoring, Gaia2 tools, scenario runtime,
replay, metrics and assertions. Ruff lint and format checks passed for the changed
authoring code and tests. The sole test warning is the existing Python `audioop`
deprecation. Runtime resources remain pinned to the isolated Gaia2 runtime hash
`eeb5257eb044a4a5b3ed98f721dba7d15608b4b6a6a8b0e5bea2ab0a29913aba`.


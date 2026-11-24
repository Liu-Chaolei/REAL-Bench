# Listener hotword implementation and validation — 2026-10-27

Current speech configurations restore `mimo-v2.5-asr` on the Agent frontend and
use `qwen-audio-3.1-asr-flash` on the evaluation-side listener. Each task prepares
and caches a fixed vocabulary before conversation time starts. Structured user
application entities are combined with source-checked free-text extraction by
the configured user model. Actual vocabulary and usage are recorded; listener
credentials and vocabulary are not forwarded to the MiMo frontend container.

The existing `0.5.2` image is reused. No full online benchmark was run.

## Checks

| Check | Result |
|---|---|
| Voice and streaming regression | 679 passed, 3 skipped; 138 live tests deselected |
| Core environment/agent/retail/runner regression | 152 passed, 2 deselected |
| Gaia2, scenario, evaluation and authoring tests | 79 passed |
| Hotword boundary tests, including later error checks | 14 passed |
| Gaia2 reference execution and strict replay | 800/800 passed; resealed |
| Existing continuation drafts in successor inventories | 8/8 reference/replay verified |
| Original task/continuation content | Preserved; base tasks change only runtime pins |
| `check-data`, configuration dry run, full Ruff lint/format | Passed |
| Standard provider suite | 132 skipped by live-provider gates |

The hotword boundary tests cover literal-source validation, unknown/reference/
future-event exclusion, Unicode and duplicate handling, stable truncation, cache
identity and concurrent reuse, caller-specific retail data, real builder/adapter
wiring, preparation failure before Agent creation, actual-playback bounds,
asynchronous recognition, fixed-vocabulary reviews, disabled-hotword controls,
credential separation and sanitized errors. The boundary test count overlaps
the earlier full voice regression and must not be added to it as unique coverage.

## Recording comparison

Four existing clips were submitted to MiMo, Qwen without hotwords, and Qwen with
their automatically prepared hotwords: 12 successful ASR requests. Gaia2 word
lists contained 1,235 and 1,148 phrases; the retail list contained 20. Word lists
were extracted from user material, not selected by the expected answer.

The sampled name-explanation audio returned variants such as “Karl Oestenson”
even with the canonical contact name present in the vocabulary. The sample does
not establish reliable recovery of accented names or isolate TTS pronunciation
from ASR errors. The time-expression sample retained “the moment”, and the order
sample retained `12345` with both Qwen settings. Some ordinary wording changed
between variants; no general accuracy or latency improvement is claimed.

Vocabulary materially increases input-token usage: one name clip reported 200
input tokens without words and 3,360 with words; the short order clip reported
45 versus 106. Per-request transcripts, latency and token usage are retained for
inspection. There is no human-verified full transcription set from which to
report a defensible aggregate WER.

## Source bindings and authoring continuity

The Gaia2 source was resealed after all 800 reference executions and strict
replays passed. Original task goals, source snapshots and resource identities
were preserved. The old series directories remain unchanged. Their successors:

- `data/tau2/task_series/gaia2/v2-asr-hotwords`: 3 existing drafts rebound and verified.
- `data/tau2/task_series/gaia2/v2-asr-hotwords-examples`: 5 drafts rebound and verified.
- `data/tau2/task_series/gaia2/v2-asr-hotwords-initial-attempt`: preserved incomplete work.

Only the active draft's base-task binding changes in the successors. Earlier
model reviews and verification records are retained as history. New bindings
require fresh model review before release; migration made no authoring/review
model calls. The pilot launcher now defaults to `v2-asr-hotwords`. These remain
authoring artifacts under the existing series workflow, not a new live benchmark.

The [usage guide](USER_ASR_HOTWORDS.md) describes configuration and artifacts.
Full evidence, runtime/task hashes, preserved earlier bindings, migration checks
and recording comparisons are under
[`data/simulations/user-asr-hotwords-validation-20261027/`](../../data/simulations/user-asr-hotwords-validation-20261027/).

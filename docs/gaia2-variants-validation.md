# Gaia2 request variants validation — 2026-11-07

The canonical `gaia2` domain now contains 1,000 three-request tasks: the 800
originals and 200 outcome-preserving request variants. No live Agent ability
evaluation was performed for this change.

## Population and content

- `mind_change`: 100 tasks, comprising 40 parameter corrections, 30 cancellations
  of temporarily added requests, and 30 selection replacements. Every change of
  mind occurs within one user utterance.
- `repetition`: 100 tasks, comprising 35 direct repetitions, 35 paraphrased
  repetitions, and 30 later-turn restatements of an earlier intention.
- Each new category draws 20 tasks from each of the original five categories.
  The 200 source tasks are distinct. All four original pilot tasks are included
  without changes to their approved text.
- Exactly one request is rewritten per new task: 112 second requests and 88 third
  requests. References, answers, criteria, evidence, checks and event dependencies
  match their sources. Original task content is preserved except runtime/resource
  pin fields written by reference sealing.
- Provenance records contain only the source ID, complete frozen source-task
  hash, variant type, subtype and modification explanation.

The frozen source is retained locally at
`data/archives/gaia2-before-variants-v1-20261107/`, including the English extension
and integrity manifest. The source task file SHA-256 is
`f2e9ada85eef24333cd71b07bd0be69c6f18d9e9dbb3e38ac7e65de9ec64d0e8`.

## Authoring and review

The existing DeepSeek profile resolved to `openai/deepseek-flash`. Generation and
independent review used separate requests, concurrency two, and a 180-second
timeout. Reviews did not receive the author's rationale. No model substitution
was performed. Final work records contain 528 model requests, including rejected
drafts and re-review, and 218 content attempts across 200 accepted tasks; no source
replacement was required.

An intermediate audit found that numeric stage indices could be interpreted as
one-based. The author and reviewer now receive the exact target utterance, its
zero-based index and its one-based number. All 200 accepted tasks passed the
updated independent review (`gaia2-variants-review-v2-explicit-target`). Misplaced
rewrites and altered quoted-title punctuation were rejected and repaired. An
additional full-population similarity audit found no obvious wrong-stage rewrite.

## Reference and runtime validation

- All 200 new tasks pass native reference execution and strict replay, with
  **3,517 authoring checks and zero failures**.
- The full **1,000/1,000** population passes reference execution and strict replay
  and is sealed as `population_version=gaia2-variants-v1`.
- The complete report contains 15,973 authoring checks and three failing checks
  on original task 0031. The previous frozen report also contained three failures
  across original tasks 0031 and 0275; these use order-sensitive participant
  pointers and their exact rows can vary with set ordering. No new variant has a
  failing authoring check. Both original and current reports remain available.
- English registry metadata contains all 1,000 task IDs. Both new category
  `tau2 run --dry-run` commands and `tau2 check-data` pass.
- Sealed validation records are rebound to the exact published task hashes.
  The frozen source hashes retain their pre-reseal meaning.

## Regression and limitations

The combined authoring, Gaia2, scenario runtime/replay/metrics, assertions and
voice integration regression passed **277 tests**. The current diagnostics suite
passed **232 tests**. The original 800-task and new 1,000-task population contracts
are both covered, along with first-request synchronization, forbidden reference
edits, incomplete export, stage indexing and sealed-record binding.

Changed Python files pass Ruff lint and format checks; `git diff --check` passes.
Repository-wide `make check-all` remains blocked by 14 existing lint errors in
`scripts/manual_fix_stage2_0074.py`. The full format check also reports that file
and `src/tau2/task_authoring/gaia2_series/manual.py`. Those unrelated files were
not rewritten to conceal the existing failures.

Artifacts: `data/tau2/task_variants/gaia2/v1/` contains the authored native tasks,
minimal provenance, complete reading sheet and validation. The canonical domain
contains the combined population and published variant validation hashes. Local
request records and check logs are under `data/simulations/gaia2-variants-v1*`.

# V5 task-aware evidence policy

Protocol `gaia2-qwen-audio/5` retains the fixed observation window and free duplex
speech, but separates official task evidence from partial-playback evidence.
V3/v4 results are immutable and must not be pooled with v5 scores.

## Official contracts

`task-contract.json` is generated offline in the pinned ARE image. It uses ARE's
resolved oracle run and the official judge's event filter, not keywords, category
names, manually supplied labels, or a classification LLM. It records required
operation/answer event IDs and counts plus source and judge-policy fingerprints.
Reference answer contents and expected argument values are not copied into this
contract. The contract is mounted read-only only in the evaluator container and
re-derived before the agent runs; it is never added to prompts or tool responses.

Types are `action_only`, `answer_only`, and `mixed`. Unsupported dynamic or
multi-turn structures remain unsupported, without substituting easier tasks.
Contract mismatch is a configuration error, not an agent capability failure.

## Scoring after interruption

- Action-only tasks submit actual operation events to the unchanged official
  judge, even if normal barge-in leaves unknown spoken text. Speech stays archived
  separately; no extra AUI answer is fabricated for an action-only task.
- Answer/mixed tasks with complete speech evidence use the whole ordered audible
  text, as in v4. All complete messages are retained; no last-answer selection.
- Answer/mixed tasks with unknown interrupted speech are `completed_unscored`,
  `task_score_status=unresolved_speech`, `success=null`, `measurement_valid=null`.
  They skip the official judge, publish no 0/1 task reward, and permit the next
  case. A later complete answer does not erase the unknown earlier speech.
- Known silence is not unknown evidence: the official judge decides whether an
  absent answer violates the task. Independently established budget failure stays
  a task failure; damaged evidence still invalidates the measurement.

`speech_evidence_status` is `complete`, `partial_interruption`, or
`missing_or_corrupt`. Normal interruption requires consistent played audio and
correlated playback-start, user-interruption, and cancellation records. Missing
files, bad sample counts, conflicting identities, or unexplained incomplete
playback remain hard audit failures. No proportional text guessing, extra ASR,
forced replay, or human annotation is introduced.

## Audit and Harbor output

The task verifier writes `reward.json`: a numeric `reward` for scored tasks and
`{}` for unresolved tasks. Null is stored in the task status file, never as a
numeric reward. V5 jobs explicitly configure `task_metric.py` using Harbor's
`uv-script` metric: default Harbor Mean treats empty rewards as zero and must not
be used for this protocol. The custom metric reports scored count, unresolved
count, score coverage and (only when available) task pass rate.

Each trial still undergoes exact artifact hashing and task-contract/projection
verification. Unresolved tasks can pass evidence auditing without claiming an
official score replay. Ordinary failures and audited unknowns continue; service,
configuration, execution-integrity and hard audit failures stop admission. Resume
revalidates saved proofs; unknown cases are not silently retried.

Reports distinguish the request's attempt limit from the full parent selection,
and show pass/fail/unknown/fault counts and coverage together. Full-scale readiness
is not implied by merely completing all diagnostic attempts.

## Preparation and validation

```bash
uv run python -m adapters.gaia2.evaluation_campaign prepare \
  --protocol gaia2-qwen-audio/5 --selection-limit 5 \
  --reuse-selection datasets/gaia2-campaign-v3-automatic-20260911-r9/manifest.json \
  --output datasets/gaia2-campaign-v5-task-evidence-20260911-smoke5
```

If uv is not on PATH, pass `--metric-uv-executable /absolute/path/to/uv`; the
scheduler supplies its directory to the Harbor child process for metric runs.
The generated configuration uses the same first five IDs, concurrency one, a
five-attempt cap, no automatic model retries, and no copied historical scores.
Preparation does not launch an agent or call a live judge.

An old 0340 trajectory may be eligible for v5 action scoring, but its v4 run has
zero saved judge calls. Any future historical re-evaluation needing semantic
checks requires fresh official judge calls; it must be labeled separately and
cannot be described as zero-call offline replay or a new agent attempt.

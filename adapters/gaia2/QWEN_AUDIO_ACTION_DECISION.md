# V7: Speech-independent failure certificates

V7 (`gaia2-qwen-audio/7`) keeps the V6 native observation, audio input, task budget, free speech, and frozen qwen source. It adds no model, manual labels, input replay, or agent-side assistance.

## Decision rule

When speech required by the task is incomplete, freeze the execution log and apply the pinned ARE 1.2.0 `GraphPerEventJudge` successful-write filter and `check_tool_call_counts` rule. Tool names come from official event objects, including official alias normalization. Reads and failed operations do not satisfy required writes.

Assume the required AUI count is satisfied only as a logical best case. Do not create an answer event or infer text. A non-AUI count mismatch proves failure regardless of unknown speech. Matching counts prove nothing about arguments, causality, answer correctness, or success: retain `completed_unscored`. The same distinction applies at the task budget cutoff. Service faults, invalid execution and damaged evidence never become agent failures through this rule.

The initial scope remains one user input, one ARE turn, no dynamic ENV/CONDITION events, and at most one official answer. Unsupported official rules fail closed.

## Evidence and result interface

- `score_basis`: `official_full_judge`, `official_action_count_failure`, or `unresolved_speech`; null for unavailable evidence.
- A proven failure has `status=completed`, `success=false`, `measurement_valid=true`, and `task_score_status=scored`. Speech stays `partial_interruption`.
- `decision-proof.json` records required and actual event IDs/tool counts, missing/extra writes, the pinned rule/source/contract hashes, and zero model calls. Its digest is included in projection and result.
- `playback-timeline.json` orders responses by actual playback start time, with cancellation/end times, samples and played duration. Numeric filenames are only storage identities. Generated transcripts are never represented as known audible prefixes.

Offline replay rebuilds the proof from the archived official events and verifies the timeline. A deterministic proof is labeled `official_necessary_condition`, distinct from a full semantic judge replay. Batch reports separate score bases, unknowns and invalid runs; unknowns retain empty rewards.

## Compatibility and rollout

V5/V6 behavior remains available. Never rewrite a historical campaign manifest, score or failed audit. Retrospective V7 comparisons live in separate directories and are not new live runs. Validation covers the archived 0084 missing-contact case, a complete 0094 full replay, count matches, failed/read/extra writes, unknown speech at timeout, tampering, and chronology.

Prepare a fresh five-task V7 campaign after offline checks. Real evaluation requires a subsequent launch; offline certificates do not establish large-scale stability or native recovery effectiveness.

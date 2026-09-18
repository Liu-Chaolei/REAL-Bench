# V8: Response-bound interruption evidence

V8 (`gaia2-qwen-audio/8`) changes evidence interpretation only. The playback implementation, qwen source, audio input, task budget, delegation, and model profile are unchanged. V5–V7 retain their prior rules.

## Association

After recording integrity and sample checks, prefer a provider `response.done` for the same response ID with `status=cancelled` and `reason=turn_detected`, paired with client playback-start and cancellation evidence. The provider terminal notification and unscoped `playback.clear` need not arrive before the client cancellation. A clear notification is not required for this primary proof.

Without that terminal proof, accept a `user_interruption` clear only if the playback lifecycle identifies one active response, followed by its cancellation receipt. A pending cancellation of an older response makes an unscoped clear ambiguous if a new response is already active; that clear cannot prove interruption of the new response.

Failed, incomplete, unknown or contradictory provider terminal states cannot be laundered through the gateway's generic `cancelled` flag. Reused response identities, conflicting turn bindings, missing receipts and ambiguous clears remain unverified. Exact duplicate records are idempotent. Association uses source log order and identities, not a relaxed time tolerance.

## Evidence and scores

`interruption-evidence.json` records per-response decisions, source row positions and hashes, and clear attribution/ambiguities. Projection and result include its digest. `playback-timeline.json` includes the association evidence alongside actual playback chronology. Offline replay recomputes the proof from original gateway logs.

A known normal interruption remains `partial_interruption`: generated transcripts are never substituted for unknown audible words. Required-action count mismatches can still produce V7's deterministic zero; matching counts leave required unknown speech unscored.

An unverified cancellation uses `speech_evidence_status=unverified_interruption`, `failure_cause=unverified_interruption`, and an invalid measurement. Damaged WAVs or inconsistent sample data remain `missing_or_corrupt`. Neither produces an agent failure score.

## Validation and rollout

Validate both historical 0084 recordings separately: the V7 recording has cancel-before-clear and lacks cab booking; the earlier V6 recording lacks contact deletion. Both retrospective V8 comparisons must preserve the original artifacts, make zero new model calls, and independently reproduce their action-count failure proofs. Complete 0094 must still replay the nine recorded official judge replies successfully.

Also cover alternative notification orders, missing clear, failed/unknown terminals, duplicate records, wrong identities, delayed clears intersecting new playback, corrupted audio, and proof tampering. Preserve the audio driver's exact bytes to verify playback behavior was not modified. Freeze a new five-case campaign; implementation validation does not launch live tests or rewrite previous results.

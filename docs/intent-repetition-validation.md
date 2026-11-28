# Intent v2 duplicate delegation validation

Implementation: `tau-real-intent/2`, duplicate rule `tau-intent-repetition/1`.
Validation date: 2026-11-07. This is a grading-pipeline acceptance check, not a
new Agent benchmark run or a semantic audit of all 800 tasks.

## Deterministic and integration checks

The diagnostic/runner/checkpoint/results/transcript regression passed **411 tests**:

```bash
UV_FROZEN=1 uv run pytest tests/test_diagnostics tests/test_runner \
  tests/test_checkpoint.py tests/test_results_format.py \
  tests/test_utils/test_transcript_views.py -q
```

After the final applicability/version-replay changes, the repetition, intent
core/schema and diagnostic integration selection passed **125 tests**. The
repetition-specific tests cover
frozen one-versus-three scoring, authorized repetitions, unknown/error states,
source quotations and JSON escapes, conditional and inline targets, shared and
cross-goal facts, source chronology, successful replay, failed-check recovery,
terminal details, version grouping, and tampered base/check/final records.

Frozen judgments with nine matched facts, six belonging to two excess goals,
produce Precision `1/3`, Recall `1`, F1 `0.5`. Content judgments remain intact;
replaying the saved final evaluation reproduces the score without model calls.

All changed Python files pass Ruff lint and format checks; `git diff --check`
passes. The container lacks system ALSA/JACK libraries required by its
installed PyAudio, so regression commands used extracted Ubuntu runtime libraries
under `/tmp/tau-repetition-system-libs/root/usr/lib/x86_64-linux-gnu` via
`LD_LIBRARY_PATH`, without changing the project environment or lockfile.

## Live DeepSeek semantic acceptance

The explicit live grading command uses the existing evaluation profile and a
private dotenv credential source. It starts no Agent and executes no business
tools. Its inputs and raw model requests are preserved under
`data/simulations/intent-repetition-v2-validation-20261107/`.

The fixed examples cover independent duplicate delegations, explicitly authorized
repetition, user emphasis, supplementary messages, final_asr/objective restatement,
different recipients/times, same-task corrections, and a later stage explicitly
requesting another occurrence. Their duplicate judgments were inspected against
their frozen original sources. All nine examples completed and met their expected
duplicate counts and extraction-goal counts.

The one-request/three-delegation example reports two excess goals, Precision
`1/3`, Recall `1`, F1 `0.5`. In this live sample, the original content judge also
recognized the excess; the deterministic frozen test independently verifies the
new adjustment when the content judge marks every fact matched.

The saved three-stage task `0006_hemtb6lwn3izyke84qxmwp2t1nrzoe2y` contains four
actual backend messages. The v2 extraction produced 12 goals and 13 source
annotations; its check found zero excess goals. Precision is
`0.8795180722891566`, Recall `1`, F1 `0.9358974358974359`. This newly generated
content score may differ from prior drafts because extraction/model judgments
were rerun; it is not evidence of a duplicate penalty. The old evidence and
historical evaluation directories remain intact.

A separate copy repeats the final accepted backend prompt under a new message,
task and session identity. It produces 15 extracted goals. The repetition check
identifies two excess goals: the retired-Galway digit-sum request and the zip-code
lookup based on that sum. Both duplicate groups and their original user/backend
citations were inspected. Three facts that the content judge had accepted become
`extra`; already-extra facts keep their status. Content Precision is
`0.8472222222222222` (61/72), final Precision is `0.8055555555555556` (58/72),
Recall is `1`, and final F1 is `0.8923076923076924`. This tests the additional
penalty on a real message structure without changing the original trajectory.

The reusable command runs nine fixed examples and these two saved-evidence cases:

```bash
UV_FROZEN=1 uv run python scripts/validate_intent_repetition.py \
  --env-file /path/to/private/config.env \
  --saved-evidence /path/to/original/diagnostics/evidence \
  --duplicate-saved \
  --output data/simulations/intent-repetition-v2-validation-20261107
```

The directory's `report.json` contains the per-case acceptance results; each case
contains copied evidence, extraction records, base and final judgments, repetition
evidence and model request/usage records. Repeating this command reuses complete
compatible records, with failed stages retried independently.

Final result: **11/11 cases passed**. A separate model-free replay reproduced all
11 saved scores exactly, and the original saved evidence manifest verified after
validation. Both original and copied-call scores remain independently inspectable.

GT is still `generated_draft` with semantic coverage `not_checked`. Acceptance
of duplicate grouping does not certify complete GT coverage or every content
judgment. Program scoring replay and live semantic review are separate checks.

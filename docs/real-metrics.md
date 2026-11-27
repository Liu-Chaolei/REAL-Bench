# REAL-derived diagnostics in τ-bench

For the current metric inventory and the modules selected by the unified
configuration, see [metrics-inventory.md](metrics-inventory.md).

The optional diagnostics suite reports whole-call intent fidelity, four execution
scores and unified voice interaction measurements alongside the task's environment
result. Interaction usefulness, state interaction and concise feedback are
additional opt-in modules. The execution adapter covers the Gaia2 tasks, including
the 200 request variants in the 1,000-task population, and their three
requests. Agent implementations share the same environment and playback evidence;
only backend-input observation needs an Agent-specific integration.

## Install and run

```bash
UV_FROZEN=1 uv sync --extra gaia2 --extra voice --extra diagnostics --extra dev

tau2 run --benchmark none --domain gaia2 --audio-native \
  --audio-native-provider qwen_agent \
  --qwen-agent-config integrations/qwen_audio_agent/config.gaia2.metrics.example.json \
  --metrics-config examples/real-metrics.yaml --num-tasks 1 --save-to metrics-example
```

Qwen backend observation requires an image containing the updated bridge. Build
it from a clean Qwen checkout, optionally reusing the existing verified runtime:

```bash
python integrations/qwen_audio_agent/build_image.py ../qwen-audio-agent \
  --base-image docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.2 \
  --tag docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.3-real-metrics
```

The existing HPC smoke helper accepts `--metrics-config` too. Models and credentials
for the evaluated Agent, caller, original reward and diagnostics remain separate.
An omitted diagnostic model inherits the run's resolved communication judge.
Explicit `model` and per-role `models` profiles take precedence; credentials use
`api_key_env`. See `examples/real-metrics.yaml`.

Changing runtime-bound code requires reference verification and sealing through
`tau2 gaia2 verify --reference --workers 8 --seal` before a Gaia2 live run.

## Saved runs

```bash
tau2 metrics run data/simulations/metrics-example \
  --config examples/real-metrics.yaml --dry-run
tau2 metrics run data/simulations/metrics-example \
  --config examples/real-metrics.yaml --only intent,execution,voice
tau2 metrics summarize data/simulations/metrics-example
tau2 metrics export data/simulations/metrics-example --output metrics.json
```

`run` resumes successful cached requests and evaluates missing/error results.
`--dry-run`, `summarize` and `export` make no model calls. Concurrency bounds the
number of independently graded saved calls. Request budgets apply per call.
Different configurations/code produce separate content-addressed profiles;
reports group different profiles instead of pooling them. The latest sidecar
is displayed by `tau2 view`, including offline evaluations.

Evidence is stored under `artifacts/task_<id>/sim_<id>/diagnostics/evidence/`.
It includes actual user input, backend submissions, original/bound tool arguments,
results, phase-state snapshots and delivered audio timelines. Its manifest checks
file hashes. Scores and model request records are in `evaluations/<profile>/`.
Model response replay validates content hashes; changing input or policy invalidates
its cache. No Agent operation is repeated to obtain a diagnostic score.

Historical calls lacking this evidence are reported as unavailable. They are not
assigned guessed state snapshots or backend messages.

## Score definitions

Intent uses one chronological record of all actually delivered user requirements
and one of all actually delivered top-level backend task messages. Both retain
message boundaries, corrections and temporal anchors. There is no lossy summary.
GT and prediction extraction are independent, and each intent is judged against
the opposite raw text. Per-call results contain Precision, Recall, F1 and
directional item explanations. The benchmark summary exposes one Intent F1
(Macro F1); Precision and Recall remain in the diagnostic details. Batch
Precision/Recall and Macro F1 give each task equal weight after averaging its
trials. Macro F1 averages task F1 values; it is not recomputed from averaged
Precision/Recall. Exact-complete match and micro F1 are retired. Coverage belongs
to evaluation logs.

### Duplicate delegations (intent v2 legacy)

The following section describes the historical `tau-real-intent/2` pipeline.
New scoring uses `tau-real-intent/3` and does not run the extra repetition check;
old v2 records remain readable with their frozen repetition judgments.

User extraction merges repeated emphasis while retaining explicitly requested
repetitions. Backend extraction retains independently issued goals even when
their business content is identical. Restated `final_asr`/`objective` fields,
history and supplementary updates do not automatically add an execution request.
Every backend goal, including conditional goals, carries external
`evidence[{path, source_id, quote}]`. These annotations do not add scoring facts.

After the ordinary bidirectional checks, one additional call to the configured
`evaluation` model inspects the complete prediction and both delivered message
streams. It identifies equivalent independent goals, the requested occurrence
count, excess goals and original-source evidence. Object identity, parameters,
conditions, time anchors and dependencies are compared together. Unknown
provenance/counts remain uncertain. No GT JSON or application state is supplied
to this check, and prediction extraction still cannot see user-side evidence.

For confirmed excess goals, matched facts whose **entire** `source_paths` belong
to that goal become `extra`, with reason `duplicate_delegation`. Existing
`extra`, `changed`, `ignored` and `uncertain` judgments retain their status.
Shared entities and cross-goal/global facts keep their original judgments.
Precision uses the final fact counts; Recall retains the original forward check;
F1 is recomputed. No second penalty multiplier is applied. A failed or uncertain
duplicate check leaves final Precision/F1 null while preserving content scores
and any known Recall. Empty predictions require no duplicate model call.

The intent directory additionally stores `evaluation.content.json` (base verdict)
and `repetition.json` (duplicate groups, citations, state and requests).
`evaluation.json` embeds their frozen records and deterministic fact adjustments,
and scoring verifies reconstruction and hashes before accepting a result.
`scores.json` and `tau2 view` show both content and final Precision, excess goals,
citations and affected facts. Summaries pool final counts and report duplicate
check coverage. Different intent versions are reported separately.

Inputs, rules, model parameters and versions determine cache identity; retries
reuse completed extraction and content checks. Each nonempty prediction normally
adds one model request, with at most one format repair under the same budget.
Old evaluations remain intact. Regrading requires original backend messages:
goals merged by an older extraction cannot be recovered from its JSON alone.

Run the fixed live judge acceptance cases and optionally a saved whole call:

```bash
UV_FROZEN=1 uv run python scripts/validate_intent_repetition.py \
  --env-file /path/to/private/config.env \
  --saved-evidence /path/to/diagnostics/evidence \
  --duplicate-saved \
  --output data/simulations/intent-repetition-validation
```

This command calls grading models only and never runs the evaluated Agent or
business tools. It keeps evidence copies and semantic acceptance results in the
specified directory. Programmatic frozen-verdict tests remain key-free.
`--duplicate-saved` grades a separate copy with the last accepted prompt repeated
under a new task/session; it never alters the collected source. `--case NAME`
selects individual acceptance cases for recovery or inspection.

Unreleased requests are excluded from intent and appear in stage coverage. A
proven empty backend submission gives F1=0 for a nonempty request. No backend
boundary is `not_applicable`; missing or unconfirmed messages are `unavailable`.
GT is marked `generated_draft`, with semantic coverage `not_checked` until reviewed.

Execution evaluates business WRITE attempts. Tool choice is one-to-one F1;
parameters use all reference checker units; order uses projected dependency edges
and actual call-start times; effects use executing app states after each operation.
Later authorized mutations do not erase a completed intermediate effect. Weights
are 25/35/10/30. N/A dimensions redistribute weights; unknown units retain their
weight as an interval. Pure query tasks use binary whole-task answer effects.
For current Gaia2 runs, necessary spoken feedback is evaluated by
`conciseness.required_coverage` when that module is selected; it is not
automatically part of the environment reward.

Reference data describes one solution path. These execution scores measure the
relation to that path and do not redefine τ outcome correctness. The original
reward remains visible even when a different valid trajectory scores differently.

New source-classified recordings report 12 metrics under `voice_interaction`,
using `tau-voice-interaction/4`: mean/P90 response latency, response rate, mean
yield latency, yield rate, interruption event/turn rates, speech overlap,
on-time rate and three selectivity rates. Ordinary means and rates give tasks
equal weight after averaging their trials. P90 uses task/trial-weighted latency
samples. On-time means response delay < 2.75 seconds, independent of tools;
yield confirmation requires one second of silence without a timeliness deadline.
See [interaction-metrics.md](interaction-metrics.md) for exact definitions.

Historical `real_voice` results retain their recorded versions and remain
separate from the unified metrics. Late rate and audible pre-tool speech are
retired from new output; historical definitions do not govern new recordings.

Infrastructure and caller errors retain diagnostic detail but do not enter
headline valid-sample aggregates. A model failure with complete evidence remains
a measured failure. Missing data is explicit; it is never silently zero/full credit.

## Extending Agents and domains

An Agent may implement the optional `DiagnosticEvidenceSource` protocol. Return
`status` (`complete`, `unavailable`, `not_applicable`), a reason and typed
`backend_inputs`. Record actual accepted top-level `prompt`/`steer` messages and
logical `submit`/`update` records separately. Mark internal child delegation as
`internal=true`. Do not generate a new task representation just for evaluation.
Qwen implements this at its original BackendPort/ACP boundaries.

A domain implements `DomainExecutionAdapter`, using the common operation/state
records. It supplies business tool metadata, reference actions/dependencies,
parameter rules and state-effect validators. This version implements Gaia2;
other domains explicitly report the execution adapter as not applicable.

## Offline validation

```bash
tau2 metrics coverage --output coverage.json
tau2 metrics verify-gaia2 --workers 8 --output /path/to/new-reference-report
UV_FROZEN=1 uv run pytest tests/test_diagnostics
```

The coverage command inventories every referenced business tool, nonempty field
and effect handler. Reference validation uses real native tools and the existing
strict replay verifier, with state capture enabled. It does not call a model;
unresolved semantic checks and timing ties remain unknown. Validation fixtures
and model ability scores are separate.

Source revisions, licenses and upstream file hashes are shipped in
`tau2/diagnostics/SOURCE.json`. The independent metric versions are
`tau-real-intent/3`, `tau-real-execution/1`, and, for new source-classified voice
records, `tau-voice-interaction/4`. Duplicate delegation checking was a legacy
τ extension (`tau-intent-repetition/1`); current intent v3 does not invoke it.
The pinned original REAL source provenance remains in `SOURCE.json`.

Implementation test results and the real frontend authentication limitation are
recorded in [validation](real-metrics-validation.md).

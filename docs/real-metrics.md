# REAL-derived diagnostics in τ-bench

The optional diagnostics suite reports whole-call intent fidelity, four execution
scores and eight waveform interaction measurements alongside the ordinary task
reward. The execution adapter covers the current 800 Gaia2 tasks and their three
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
the opposite raw text. The output contains Precision, Recall, F1, exact-complete
match, directional item explanations, micro/macro summaries and coverage.

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
Necessary speech in mixed tasks continues to be checked by the original reward.

Reference data describes one solution path. These execution scores measure the
relation to that path and do not redefine τ outcome correctness. The original
reward remains visible even when a different valid trajectory scores differently.

`real_voice` reports mean/P90 response latency, response rate, interruption rate,
speech overlap, on-time/late rates and audible pre-tool speech. It uses the pinned
REAL VAD/resampler and threshold rules, with τ's actual media delivery/playback
clock and delivered user turns. These are versioned separately from τ's original
similarly named metrics. Rates pool event counts; P90 pools latency samples.

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
`tau-real-intent/1`, `tau-real-execution/1`, and `tau-real-voice/1`.

Implementation test results and the real frontend authentication limitation are
recorded in [validation](real-metrics-validation.md).

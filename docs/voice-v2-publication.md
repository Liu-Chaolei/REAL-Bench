# Voice V2 release packages

The frozen 2026-10-02 release calls predate the current matrix layout and some
recorded condition fields. The maintainer-only historical adapter reads the saved
export at `trajectories/<language>/<domain>/` and creates the same reward package
as a current matrix submission. It never runs a simulation or calls a model.

The independent Judge suite has been removed. Current packages do not require,
read, generate or upload Judge verdicts or `benchmark-diagnostics.json`.
Previously published files remain historical artifacts and are not rewritten.

## Inputs and commands

Provide the export, including each cell's `results.json`, `simulations/`, recorded
resources and optional `correction.json`, plus submitter metadata. No Judge root
is needed.

```bash
R=data/simulations/release_runs/2026-10-02

tau2 submit prepare $R/leaderboard-v2-corrected-2026-10-09/<provider-export> \
  --historical-voice-v2 --metadata-file publication-metadata.json --output ./packages

tau2 submit validate ./packages/<name> --historical-voice-v2 \
  --results $R/leaderboard-v2-corrected-2026-10-09/<provider-export>
```

The package contains `benchmark-submission.json`, `benchmark-metadata.json` and,
for the core voice-v2 frame, `benchmark-quality.json`. Quality is call provenance
(simulation ID, task ID and termination reason), without independent performance
scores. The package records `evidence: historical-voice-v2-2026-10-02`.

## Existing evidence checks

The adapter keeps the existing reward, frame, condition and trajectory checks.
It reads recorded JSON so historical provider strings remain readable without
relaxing new-run configuration. The result index must agree with the saved
simulation files. Unscored calls must be recorded infrastructure errors; the
existing correction manifest must still describe the corrected files. Agent and
caller identity and shared run conditions must agree across cells.

The core frame reads its recorded benchmark binding. Banking and entity calls
predate their manifests, so the adapter derives the frame from released bindings
and checks the recorded artifact pins and existing `ACCEPTED_RUNTIME_RULES`
digests. Removing Judge diagnostics does not change those reward checks or add
new checks to benchmark execution.

## Banking and entity

Select `--benchmark banking` or `--benchmark entity` with the same historical
adapter. Their source paths are `trajectories/<language>/banking/` and
`trajectories/<language>/intake/regular/`. Package names append `_banking` or
`_entity`. Entity also includes `benchmark-pass3.json` from the regular,
channel-heavy and speech-heavy conditions. Banking and entity do not carry the
core voice-v2 call-provenance sidecar.

## Review and upload

```bash
python -m tau2.scripts.leaderboard.review_submission \
  ./packages/<name> <provider-export> --historical-voice-v2
```

The command prints a review/upload plan. Add `--upload` when publishing is
intended. Trajectories retain the language/domain layout, `correction.json` maps
to `source_correction.json`, and entity's extra conditions map to
`pass3/<language>/intake/<condition>/`. Judge evidence is no longer uploaded.
See the [maintainer guide](../src/tau2/scripts/leaderboard/MAINTAINER.md).

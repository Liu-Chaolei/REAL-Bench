# Gaia2 request variants

The `mind_change` and `repetition` categories add 100 tasks each to the five
original categories (160 tasks each). The combined population contains 1,000
three-request conversations. Each new task copies a different original task and
rewrites exactly one user request while retaining its final business objective,
reference tools, reference answers, criteria, evidence and offline checks.

## Authoring

`mind_change` contains 40 parameter corrections, 30 cancellations of a temporarily
added request, and 30 selection replacements. Every correction happens within one
utterance and ends at the original request's objective. `repetition` contains 35
direct repetitions, 35 paraphrased repetitions, and 30 later-turn restatements of
an earlier intention. Restatements use the existing caller and stage logic.

Each new category draws 20 tasks from each original category. The two groups use
200 distinct originals. The four reviewed examples in the historical
`data/archives/gaia2/trials/gaia2-variants-pilot-v1/` bundle counted toward these
quotas during authoring. Selection uses
seed 300 and excludes source tasks with known failing authoring checks.

The author and independent reviewer use the existing DeepSeek configuration at
`docs/gaia2-series.deepseek.config.json`: concurrency two, a 180-second request
timeout, and three content attempts per candidate. Provider failures do not consume
content attempts or switch models. Models return only rewritten text and a short
explanation; native task construction is deterministic. Review does not receive
the author's explanation. Temporary alternatives must exist in scene materials.

```bash
export UV_FROZEN=1
python -m tau2.task_authoring.gaia2_variants prepare
python -m tau2.task_authoring.gaia2_variants run
python -m tau2.task_authoring.gaia2_variants export
python -m tau2.task_authoring.gaia2_variants merge
tau2 gaia2 verify --reference --workers 16 --seal
python -m tau2.task_authoring.gaia2_variants finalize
tau2 check-data
```

Use `--source` to select a frozen 800-task archive, `--dataset` for the authored
dataset, `--work` for resumable temporary records, and `--config` for the existing
authoring configuration. Credentials remain environment references. Archive the
canonical data and its English language extension before merging or resealing.

## Artifacts and validation

The native `Task` schema has no new fields. New IDs use
`<original-id>__mind_change_v1` or `<original-id>__repetition_v1`, and
`scenario.category` identifies the new category. Upstream source IDs and source
hashes retain their original meaning. Initial-request rewrites update
`reason_for_call` as well as the stage and scripted user event.

The authored dataset contains `tasks.json`, `provenance.json`, `review.md` and
`validation.json`. Provenance records only source task ID, frozen source-task
SHA-256, variant type, subtype and modification explanation. The dataset manifest
identifies the frozen source and selection. Source-task hashes use the complete
canonical JSON before subsequent runtime resealing.

Structural comparison rejects any unapproved change to references or scoring.
Independent semantic review checks equivalent objectives, retained conditions,
grounding, no answer leakage, natural language and context consistency. Each new
task must also pass the existing reference execution, authoring checks and strict
replay. These checks validate construction; they are not model ability scores.

The combined manifest declares `population_version=gaia2-variants-v1`. Validation
accepts either the historical five-category 800-task population or the complete
seven-category 1,000-task population. Fixed async tasks have been removed;
`base` selects the 800 original tasks and `all` selects all 1,000 tasks. The original
five category splits retain their original membership, and the two new splits select
their 100 variants. No separate `original` split is added.

```bash
tau2 run --benchmark none --domain gaia2 --language en \
  --task-split-name mind_change --num-tasks 5
tau2 run --benchmark none --domain gaia2 --language en \
  --task-split-name repetition --num-tasks 5
```

The Parquet importer continues to import only the original five configurations.
Runtime scoring, user simulation, STOP-driven stage delivery and provider settings
are shared with ordinary Gaia2 tasks. Historical source assertion failures remain
visible in the combined verification report; new variants must introduce none.

The [validation record](gaia2-variants-validation.md) describes the completed
1,000-task seal, review corrections, checks and existing repository limitations.

# Glossary

Terms used across the τ-bench 2.0 docs, in alphabetical order.

## Benchmark manifest

A JSON file under `data/tau2/benchmarks/` that defines a released benchmark:
its version, call mode (text or voice), the frozen task subset for each domain,
and the list of [cells](#cell) with their [binding revisions](#binding-revision). Three manifests ship with 2.0:

| File | Benchmark version | Mode | Cells |
|---|---|---|---|
| `text.json` | `text-v1` | text | 6 languages × 3 domains = 18 |
| `voice.json` | `voice-v2` | voice | 6 languages × 3 domains = 18 |
| `voice-v1.json` | `voice-v1` | voice | English × 3 domains = 3 |

`tau2 benchmarks list` prints them. `tau2 run --benchmark <name>` selects one by
file name (`text`, `voice`, `voice-v1`).

## Binding revision

A SHA-256 fingerprint of everything a Gaia2 binding feeds into a call: the task
content, caller prompt, runtime rules, assets, and call mode. A run refuses to
start if the current checkout no longer produces the binding revision recorded
by the manifest.

## Cell

A selected Gaia2 task and its language/runtime binding. New results are stored
under `data/results/<run-name>/`.

## Frame

The fixed Gaia2 task population and trial indices used for a benchmark score. A
run records its selected canonical task IDs and benchmark metadata in its result
root. Infrastructure failures remain explicit error trials.

## Generation quality

A retired measure (serialized as `experience` in historical releases). It combined
Naturalness and Speech Fidelity judgments. Current evaluation no longer computes
it; historical paper and release results retain their original definitions.

## Interaction

Current `benchmark-quality-v3` packages retain the independent **selectivity**
flag: more than 3 errors summed over backchannels, vocal tics and non-directed
speech. It is computed from saved ticks and is unknown without an opportunity.
There is no combined call-level verdict or tool-misuse flag. The three detailed
selectivity metrics and the TICK/media voice panels keep their existing rules.

## Measure

One named evaluation quantity. Current measures and aggregation rules are listed
in [the metrics inventory](metrics-inventory.md). The independent Judge suite has
been removed; task evaluators and current diagnostics remain separate.

## Binding

Gaia2 resolves an English caller binding from `src/tau2/domains/gaia2/language_runtime.py`. The binding records the prompt, caller settings, task selection, and content fingerprints used by a run.

## Pin

A value recorded in a manifest so that later runs can be checked against it: the subset digest of each task subset and the binding revision of
each cell. `tau2 benchmarks pin <name>` recomputes pins from the checkout and
refuses to change them unless you also pass a new `--revision`.

## Trial

One run of one task. `--num-trials N` runs each task N times with different
seeds. The released benchmarks are scored at one trial (pass@1);
`pass^k` is the share of tasks passed in all k trials.

## Historical benchmark names

τ-i18n/τ-Multilingual and τ-Entity/elicitation are historical benchmark names. They remain readable in old serialized records but are not active run targets.

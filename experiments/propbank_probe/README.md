# PropBank probe for Gaia2

This is an isolated experiment for checking how much predicate-argument
structure an English PropBank SRL model can recover from Gaia2-style requests.
It does not change tau2's intent extraction or scoring pipeline.

The experiment has three layers:

1. Run an SRL backend and preserve its raw predicate/argument output.
2. Normalize the output into stable predicate and span records.
3. Apply a small, explicit Gaia2 action mapping and deterministic connective
   rules for conditions and temporal relations.

The default real backend is HanLP. The fixture backend used by the tests does
not download a model.

## Setup

```bash
cd experiments/propbank_probe
uv sync
```

HanLP downloads its selected model on first use. The model identifier can be
passed explicitly with `--model`. When no identifier is supplied, the runner
searches for the documented English SRL model constants exposed by HanLP.

## Run the offline smoke path

```bash
PYTHONPATH=src uv run python -m propbank_probe.cli parse \
  --input data/cases.jsonl \
  --backend fixture \
  --output results/fixture-run

PYTHONPATH=src uv run python -m propbank_probe.cli map \
  --input results/fixture-run/normalized.jsonl \
  --mapping-config data/gaia_mapping.yaml \
  --output results/fixture-run

PYTHONPATH=src uv run python -m propbank_probe.cli report \
  --predictions results/fixture-run/normalized.jsonl \
  --mapped results/fixture-run/mapped.jsonl \
  --composition results/fixture-run/composition.jsonl \
  --gold data/expected.jsonl \
  --output results/fixture-run/report.md
```

The real backend uses the same command with `--backend hanlp`.

## Output

Each run contains `manifest.json`, `raw.jsonl`, `normalized.jsonl`,
`mapped.jsonl`, `composition.jsonl`, `errors.jsonl`, and a report. Raw backend
payloads and source spans are retained so that a failed mapping can be traced
back to the original text.

The parser may not emit a PropBank roleset such as `give.01`. The probe records
that fact as `sense_status=not_emitted` and does not infer a sense from the
lemma. Mapping ambiguity is preserved in the report.


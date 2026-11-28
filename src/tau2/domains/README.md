# Gaia2 Domain

The active domain implementation is `src/tau2/domains/gaia2/`; hidden-intent
loading lives in `src/tau2/domains/gaia2_hidden/`.

## Structure

- `data_model.py` and `tools.py` define the domain models and application tools.
- `environment.py` exposes `get_environment()`, `get_tasks()`, and task splits.
- `language.py` and `language_runtime.py` resolve the English caller binding.
- `recorded.py` handles strict reads of historical Gaia2 archives.
- `verify.py` and `utils.py` enforce task, resource, and runtime fingerprints.

Gaia2 tasks use content-addressed snapshots and scenario events rather than a
single mutable domain database. Data lives in `data/tau2/domains/gaia2/` and is
sealed by `manifest.json`. After changing task, resource, or runtime files, run:

```bash
UV_FROZEN=1 uv run tau2 check-data
UV_FROZEN=1 uv run tau2 gaia2 verify --reference --workers 8 --seal
```

`evaluation_criteria.actions` is one reference trajectory used to derive the
target state. It is not a per-call requirement unless the task explicitly uses
`RewardType.ACTION`; see [evaluation](../../docs/evaluation.md).

## Tests

Domain tests live in `tests/test_domains/test_gaia2/` and
`tests/test_domains/test_gaia2_hidden/`:

```bash
pytest tests/test_domains/test_gaia2
pytest tests/test_domains/test_gaia2_hidden
```

Use Gaia2 task IDs in new fixtures. Use small in-memory doubles for generic
protocol tests. Historical τ-bench domain implementations and their tests have
been retired from the active registry.

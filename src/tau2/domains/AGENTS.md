# AGENTS.md — src/tau2/domains/

The active domain is Gaia2. New domain changes should target
`src/tau2/domains/gaia2/` and register through `src/tau2/registry.py`.

## Gaia2 files

- `environment.py` — environment and task loaders.
- `tools.py` — application tools and state transitions.
- `language.py` / `language_runtime.py` — English caller binding.
- `recorded.py` — strict historical archive reads.
- `verify.py` / `utils.py` — manifest, task, resource, and runtime checks.

Task data lives under `data/tau2/domains/gaia2/`. Treat tasks, snapshots,
resources, and manifests as sealed inputs. After changes run `tau2 check-data`
and the relevant `tau2 gaia2 verify --reference --seal` command.

Gaia2 tasks use content-addressed application snapshots and scenario events.
Environment construction must preserve the task's runtime mode and verification
options; do not silently skip a seal check in production paths.

## Tests

Use `tests/test_domains/test_gaia2/` and
`tests/test_domains/test_gaia2_hidden/`. Use real Gaia2 task IDs for domain
fixtures and local doubles for generic tool/protocol tests. Retired τ-bench
domain implementations are not active registry targets.

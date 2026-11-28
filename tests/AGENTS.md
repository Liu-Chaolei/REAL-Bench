# AGENTS.md — tests/

The active test suite targets Gaia2, shared runner/orchestrator behavior, diagnostics,
voice providers. Retired τ-bench domain tests and old
benchmark matrix tests are not part of the repository.

## Layout

- `test_domains/test_gaia2/` and `test_domains/test_gaia2_hidden/` — active task and environment tests.
- `test_runner/` — checkpoints, workers, benchmark selection, and run contracts.
- `test_diagnostics/` — offline diagnostic measures and evidence boundaries.
- `test_voice/` and `test_streaming/` — audio-native and full-duplex behavior.
- `test_task_authoring/` — Gaia2 authoring and validation pipeline.

## Commands

```bash
make test
make test-diagnostics
make test-voice
make test-all
```

Provider and live-model tests remain gated by their existing environment variables
or pytest markers. The default core suite must not require API keys.

## Conventions

Use real Gaia2 task IDs for domain and runner tests. Use local doubles for transport,
LLM, and provider protocol tests. Do not add new fixtures for retired domains or
reintroduce old task IDs such as `create_task_1`. Preserve saved-trajectory and
retirement tests when they validate compatibility readers rather than active runs.

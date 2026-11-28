# AGENTS.md

> Current repository guidance for coding agents working on REAL-Bench.

## Project scope

The active benchmark is Gaia2. The registry exposes the `gaia2` domain and the
`gaia2_hidden` task set. Historical τ-bench domains, multilingual packs, judges,
and task subsets are retained only when a saved-result compatibility path names
them explicitly. New code and documentation should target Gaia2 and English.

The canonical Qwen implementation lives under
`src/tau2/adapters/qwen_audio_agent/`. Use that path in new imports. The old
`src/tau2/voice/audio_native/qwen_agent/` compatibility package has been removed.

## Setup and commands

```bash
UV_FROZEN=1 uv sync --extra dev
UV_FROZEN=1 uv sync --extra voice --extra dev
UV_FROZEN=1 uv sync --all-extras
UV_FROZEN=1 uv run tau2 check-data
```

Run text and voice smoke tests with the Gaia2 configs:

```bash
UV_FROZEN=1 uv run tau2 run --domain gaia2 \
  --agent-llm gpt-4.1 --user-llm gpt-4.1 --num-tasks 1

integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1 --max-concurrency 1
```

Results from new runs go under `data/results/`. Historical `data/simulations/`
and `data/tau2/results/final/` artifacts are not active runtime inputs.

## Architecture

- `src/tau2/agent/` — half-duplex and full-duplex agent protocols.
- `src/tau2/domains/gaia2/` — Gaia2 environment, tools, scenarios, and task loading.
- `src/tau2/domains/gaia2_hidden/` — hidden-intent task loading.
- `src/tau2/adapters/` — external Agent adapters, including Qwen.
- `src/tau2/diagnostics/` — intent, execution, voice, interaction, state, and conciseness measures.
- `src/tau2/runner/` — batch execution, checkpoints, workers, and benchmark plans.
- `src/tau2/voice/` — synthesis, transcription, audio effects, and native providers.
- `integrations/qwen_audio_agent/` — local Qwen build/runtime integration.
- `requirements/bridge/` — published bridge bundle; keep it synchronized with its release workflow.

Every active domain must be registered in `src/tau2/registry.py`. Gaia2 data is
sealed by its manifest; run reference verification after changing task, resource,
or runtime files.

## Testing

```bash
make test                 # core tests
make test-diagnostics     # diagnostics tests
make test-voice           # voice and streaming tests
make test-all             # all installed tiers
make check-all            # Ruff lint and formatting
```

Tests that call live providers must use the existing `live_api` or provider-specific
gates. Do not add credentials or generated runs to the repository. Use Gaia2 task
fixtures for domain behavior and small in-memory doubles for protocol tests.

## Code style and safety

Use Python 3.12+, Pydantic models for serialized data, and Ruff's configured rules.
Keep public APIs documented. Treat `data/tau2/domains/gaia2/` and benchmark manifests
as sealed data: make data changes deliberately and run `tau2 check-data` plus the
relevant Gaia2 verification command.

Preserve historical serialized fields and compatibility readers when they are still
needed for saved results. Remove dead runtime paths only after checking imports,
CLI registration, tests, and documentation references.

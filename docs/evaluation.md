# Gaia2 Evaluation

Gaia2 evaluation combines the environment state result with the active diagnostic
profiles. The environment evaluator replays the task's reference actions in a
fresh Gaia2 environment and compares the resulting state with the simulation
state. Equivalent tool trajectories can pass when they produce the same target
state.

The active diagnostics are configured in `examples/real-metrics.unified.yaml`:

- `intent` measures requirement coverage and alignment.
- `execution` measures tool selection, parameters, ordering, and effects.
- `voice` measures timing, responsiveness, interruptions, and selectivity.
- `interaction` and `state_interaction` measure turn behavior and progress.
- `conciseness` measures required feedback and length efficiency.

Missing evidence is recorded as unavailable. It is not silently converted to a
perfect score or an ordinary model failure. Saved results retain the task,
runtime, binding, and diagnostic profile versions needed for later inspection.

Use the ordinary CLI for a fresh run:

```bash
UV_FROZEN=1 uv run tau2 run --domain gaia2 \
  --agent-llm gpt-4.1 --user-llm gpt-4.1 --num-tasks 1
```

Use `tau2 metrics run` to evaluate missing or failed diagnostic profiles on a
saved result. Gaia2 reference verification and seal checks are separate from
Agent quality: a valid seal confirms the benchmark inputs, not the score.

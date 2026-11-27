# Compact benchmark

`compact` is a 100-task frame selected from the V2-repair and three-run
intersection. It keeps one task per source family:

- 70 base tasks;
- 8 `mind_change` tasks and 7 `repetition` tasks;
- 8 `addtask`, 4 `modify` and 3 `cancel` tasks.

The frozen selection is recorded in
`data/tau2/benchmarks/compact.json`. The selection file references the
canonical Gaia2 task sources and records their SHA-256 pins; it does not copy or
rewrite task bodies. Its `conditions.task_records` section records each selected
task's `source_task_id`, which makes the no-duplicate-family rule auditable.

Run the compact frame with:

```bash
tau2 run --benchmark compact --agent-llm gpt-4.1 --user-llm gpt-4.1
```

Use `tau2 benchmarks list` to inspect the installed full and compact manifests.

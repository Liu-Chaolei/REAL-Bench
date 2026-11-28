# Running Gaia2 Simulations

当前运行器只接受活动的 Gaia2 任务集。文本和语音都使用英文 Gaia2 binding，并把新结果写入 `data/results/`。

## CLI

```bash
UV_FROZEN=1 uv run tau2 run \
  --domain gaia2 \
  --agent-llm openai/gpt-4.1 \
  --user-llm openai/gpt-4.1 \
  --task-ids 0002_46myz87t501b0c902dy81rlbqbye2dyi \
  --num-trials 1 \
  --save-to gaia2-text-one
```

语音运行通过 Qwen Audio Agent 集成：

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1
```

`--dry-run` 查看计划，`--resume` 继续未完成批次，`--task-ids` 固定 canonical task ID。
首次运行前确认 Gaia2 manifest 已完成 reference verification 和 seal。

## Python API

```python
from tau2 import TextRunConfig
from tau2.runner import get_tasks, run_single_task

config = TextRunConfig(
    domain="gaia2",
    agent="llm_agent",
    llm_agent="openai/gpt-4.1",
    llm_user="openai/gpt-4.1",
    task_ids=["0002_46myz87t501b0c902dy81rlbqbye2dyi"],
)
task = get_tasks("gaia2", task_ids=config.task_ids)[0]
result = run_single_task(config, task, seed=42)
print(result.task_id, result.reward_info.reward)
```

需要批量运行时使用 `run_domain(config)`；需要自定义并发、checkpoint 或 worker 时使用
`run_tasks(config, tasks, save_path=..., save_dir=...)`。实例级构建入口见
`tau2.runner.build`。

## 查看结果

```bash
UV_FROZEN=1 uv run tau2 view --file data/results/gaia2-text-one
UV_FROZEN=1 uv run tau2 metrics run data/results/gaia2-text-one \
  --config examples/real-metrics.unified.yaml
UV_FROZEN=1 uv run tau2 metrics summarize data/results/gaia2-text-one
```

完整参数见 [CLI reference](cli-reference.md)，数据验证见 [Gaia2 guide](gaia2.md)。

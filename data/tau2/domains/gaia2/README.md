# 当前 Gaia2 数据目录

唯一活动主文件为 `tasks.json`，共 **1000 道三阶段任务**：

| split | 数量 | 内容 |
| --- | ---: | --- |
| `base` | 800 | 原始五类基础任务，各类 160 道 |
| `mind_change` | 100 | 与基础题共享目标参考的改口变体 |
| `repetition` | 100 | 与基础题共享目标参考的重复表达变体 |
| `all` | 1000 | 上述三个集合的并集 |

五个原始类别 split 继续保留。固定的 `async` 100 题和 `persistent_tasks/`
下的 10 道样例已移除，不再保留对应 split、ID 映射或固定参考材料。

异步交互仅采用运行时动态机制：`active_metrics.persistent_task`。它在真实后台任务
状态下选择意图、动态生成请求、记录实际结果并评分，不需要独立异步题库。

相关位置：

- 动态机制：`src/tau2/metrics/active/persistent_task/`。
- 配置：`examples/active-metrics.persistent-task.yaml`。
- 公共语言回退模板：`data/tau2/metrics/persistent_task/templates/en.json`。
- 使用说明：`docs/gaia2-async.md`。

`manifest.json` 的任务数、分类和文件哈希与保留集合同步；`verification.json`
明确记录本次是删除子集后继承未变任务的历史结果，没有重新执行参考验证，也没有更新
运行时或资源封存。保留的 1000 道题内容、目标参考、快照和资源未改写。

隐藏信息 100 题仍在相邻的 `data/tau2/domains/gaia2_hidden/` 中，独立运行。

## Modify / Cancel / AddTask 变体

新增交互变体保存在 `interaction_variants.json`，通过来源题 ID 和完整来源哈希
加载为运行视图。`tasks.json` 继续保留原来的 1000 条记录。对应 split 为
`addtask`（100）、`modify`（50）、`cancel`（50）；有此文件时，`all` 加载1200题。
来源与生成信息见 `interaction_variants.manifest.json`。

这三类变体自动使用动态后台观察与用户交付机制，不需要通用随机 Query／Result
触发配置。Cancel 先追加临时 B，再取消 B，原任务 A 继续。目标不能可靠绑定时
保留未绑定状态；不替 Agent 拆任务或回滚业务操作。

Modify／Cancel 不调用原 ENV、参考路径执行质量或状态交互评分，整题奖励为空值。
AddTask 使用补齐后的参考，允许计算 ENV、执行质量与状态交互；诊断模型仍由
`--metrics-config` 选择。原题行为保持原定义。

离线构造使用 `docs/gaia2-series.deepseek.config.json` 中的原有 DeepSeek 环境引用。
生成记录、内容修订与阅读对照保存在
`data/archives/gaia2/authoring/gaia2-interaction-variants-v1/`。
该批数据为生成草稿，构造与本地检查不等于真实 Agent 的整批评测结果。

```bash
UV_FROZEN=1 .venv/bin/python -m tau2.task_authoring.gaia2_interactions
# 只导出现有记录，不调用模型：
UV_FROZEN=1 .venv/bin/python -m tau2.task_authoring.gaia2_interactions --export-only --publish
tau2 run --benchmark none --domain gaia2 --task-split-name modify --language en \
  --audio-native --audio-native-provider qwen_agent \
  --qwen-agent-config integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json \
  --metrics-config examples/real-metrics.deepseek-all.yaml --num-tasks 1
```

常规 `tau2 gaia2 verify --reference --seal` 仍只针对主文件，避免把变体运行视图写回
来源题库。新增 B 的隔离参考检查是离线构造命令
`python -m tau2.task_authoring.gaia2_interactions --check-added-references`，不会执行原题参考。

# 动态异步任务与证据适配接口

当前异步交互只采用 `active_metrics.persistent_task` 的运行时动态机制。
固定 async100题和 `data/tau2/domains/gaia2/persistent_tasks/` 的10题材料已移除。
普通主任务总数为1000，不再通过独立 `async` split 选择异步测试。

Modify／Cancel／AddTask 使用同一动态机制的题目指定分支，交互差异保存在独立
`interaction_variants.json`，不恢复旧固定 async 或 qpt 材料。三类 split 自动安装
题目运行模式；每题仅执行其已保存的业务变化。见
[实施计划](modify-cancel-addtask-plan.md) 与
[数据说明](../data/tau2/domains/gaia2/README.md)。

## 适配边界

```text
后台真实事件 → Provider Adapter → AsyncEvidenceSource
→ 动态触发策略 → 请求生成与用户交付 → 操作/结果/回复评分
```

待测 Agent 可提供 `get_async_evidence_source()`，暴露 enable、capabilities、
drain_events 和 status。公共机制观察任务身份、状态、操作和回复，不替 Agent 执行任务。

Qwen 的原生观察器仍在 `integrations/qwen_audio_agent/`；其他 Agent 通过同一接口适配。
内部任务/调用/回复 ID 按会话关联，实际观察时间映射到运行时间线。

## 数据与配置

- 主任务：`data/tau2/domains/gaia2/tasks.json`，base800＋两类变体各100。
- 动态配置：`examples/active-metrics.persistent-task.yaml`。
- 公共模板：`data/tau2/metrics/persistent_task/templates/en.json`。
- 运行模块：`src/tau2/metrics/active/persistent_task/`。

动态请求的参考来自已经选定的任务/操作意图与当时实际返回的结果，不需要预写100条请求。
动态指标只报告 `completion_score` 后台请求完成分；后台结果与回复判断保存在 `evaluation_log.native_result` 和 `evaluation_log.reply`，不作为独立性能分；
`operation_reward` 留在日志，不额外乘一次，也不默认乘入普通任务结果。

历史配置、轨迹和结果的兼容代码保留供读取历史材料；当前活动数据不依赖固定 sidecar。

使用示例见 [运行说明](gaia2-async.md)，详细规则见
[动态异步机制](active-persistent-task-metric-plan.md)。

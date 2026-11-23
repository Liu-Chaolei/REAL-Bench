# 运行时动态异步交互

异步交互只保留运行时动态机制。主数据目录没有固定 `async` split，原 100 道 async
变体和 10 道 Persistent Tasks 固定样例材料已经移除。

主文件 `data/tau2/domains/gaia2/tasks.json` 保留1000题：base800、mind_change100、
repetition100。动态异步可挂载到普通任务，不额外增加数据集题数。

## 启用方式

沿用普通 Agent 运行配置，并添加：

```bash
--active-metrics-config examples/active-metrics.persistent-task.yaml
```

例如现有 Qwen 接入可以使用：

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json \
  --active-metrics-config examples/active-metrics.persistent-task.yaml \
  --task-ids 0052_snsq4rd73gytoo0wr75bgd2nyzra07z1 \
  --max-seconds 180 --save-to gaia2-dynamic-async-01
```

这是配置示例，本次数据整理不执行真实模型测试。

## 运行过程

1. 公共适配接口观察真实后台任务和状态。
2. 动态指标模块选择可用状态与请求意图。
3. 按当时上下文生成请求，通过普通用户音频路径发送。
4. 记录实际后台操作、结果和对应回复。
5. 分别计算结果与回复分数，保留运行日志。

公共语言回退模板保留在 `data/tau2/metrics/persistent_task/templates/en.json`。
它们按请求类型复用，不是固定题库。

运行参数、证据和当前机制见 [动态异步机制](active-persistent-task-metric-plan.md)。
旧固定 qpt 运行与报表属于历史记录，不再使用旧 ID 或固定数据脚本发起新评测。

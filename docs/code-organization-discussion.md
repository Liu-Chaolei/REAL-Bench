# τ-bench 代码整理当前状态

本文已由最初的讨论稿改为当前实现清单。详细实施记录见[代码整理收尾计划](code-organization-finalization-plan.md)，指标定义以[指标清单](metrics-inventory.md)为准。

## 1. 当前范围

当前新运行只支持 `gaia2`、英文及文本／语音模式。隐藏信息通过独立 task set 选择，共用 Gaia2 环境。

本轮完成未完整适配 Provider 的移除、Gaia2 主入口旧领域清理、状态文档更新，以及参考修复产物与活动版本的对应。错误／重试／统计口径、默认采集及指标事实依据仍暂缓。

## 2. 当前运行链路

```text
CLI / RunConfig → 发布清单与任务选择 → Runner 装配
    → Gaia2 Environment / ScenarioRuntime
    → Orchestrator + 模拟用户 + 待测 Agent Adapter
    → 保存轨迹与公共证据
    → 主评分、active metric、场景变体评分、诊断评分
    → 保存各自结果并汇总
```

关键入口：`src/tau2/cli.py`、`src/tau2/runner/build.py`、`src/tau2/runner/simulation.py`。

场景机制各有职责：

| 机制 | 当前职责 | 实现位置 |
| --- | --- | --- |
| ScenarioRuntime | 场景事件、环境变化及其依赖 | `environment/scenario.py`、`domains/gaia2/replies.py` |
| ScenarioStages | 收到普通用户 STOP，等待请求完整交付、输出和工具排空后，推进下一阶段 | `orchestrator/scenario_stages.py` |
| InteractionVariant | modify／cancel／addtask 请求与任务视图 | `metrics/scenario_mechanisms.py`、`domains/gaia2/interaction_variants.py` |
| Active persistent task | 根据真实后台事件动态提出 query／result／modify／cancel 请求，独立评分 | `metrics/active/persistent_task/` |
| 旧 persistent task | 读取历史证据与已保存分数 | `persistent_tasks/` 中的兼容数据与证据接口 |

Stages 与 Runtime 的职责可以并存。动态用户请求属于测试条件；它的来源与普通阶段、环境脚本分别记录。参考事件在特定环境因果中的作用见[链路审查](evaluation-chain-audit-report.md)，该文保留审查时的背景，不作为当前指标定义。

## 3. 已完成整理

| 事项 | 当前状态与实现位置 | 详细说明 |
| --- | --- | --- |
| API 角色配置 | 服务、模型、调用角色从统一配置解析；`utils/api_config.py` | [API 配置](api-configuration.md) |
| Qwen 适配层 | 实际实现集中到 `adapters/qwen_audio_agent/`，`adapters/registry.py` 延迟创建；原路径只作兼容转发 | [Adapter 入口](../src/tau2/adapters/README.md) |
| 用户策略归属 | 动态追问等归入 `UserSimulationConfig`，旧 Qwen 配置仅在读取边界转换 | [Qwen 迁移计划](qwen-agent-adapter-migration-plan.md) |
| GPT Live／Gemini Live | `openai_live`、`gemini` 的运行入口及专属实现已移除；未来重新完整接入。普通 `openai` 保留 | [Provider 说明](../src/tau2/voice/audio_native/README.md) |
| 历史 Provider 读取 | `RecordedAudioNativeConfig` 保留历史字段；不含连接能力，恢复退役 Provider 明确拒绝 | `data_model/run_config.py`、`runner/checkpoint.py` |
| Gaia2 主入口 | 新配置移除 retrieval／elicitation 设置，旧输入明确拒绝；Runner 不再传递旧领域装配参数 | `data_model/simulation.py`、`runner/build.py`、`runner/helpers.py` |
| 历史领域数据 | Info 保留旧字段，leaderboard 明确标为历史展示；外围独立模块不顺带重写 | `data_model/simulation.py`、`cli.py` |
| 交互变体与指标分离 | InteractionVariant 由场景机制管理器安装，普通 active metric 单独管理 | [变体分离计划](interaction-variant-separation-plan.md) |
| 旧 Persistent Task 退役 | 新运行不再接受 `persistent_task_config`；历史数据和公共后台证据类型保留 | [退役计划](persistent-task-retirement-plan.md) |
| 请求来源统一 | 运行记录及诊断读取 `source`、`purpose`；阶段、变体及 active 请求来源对应 | `environment/scenario.py`、`diagnostics/collect.py` |
| 公共媒体证据 | 精简性与语音指标直接读取公共媒体证据 | `diagnostics/media.py` |
| 先保存事实再评分 | 原始轨迹、证据先保存，各评分独立执行；不以另一项分数充当原始证据 | [证据与评分分离计划](evidence-and-scoring-separation-plan.md) |
| 指标清理 | Gaia2 主业务结果保留 ENV_ASSERTION；所需反馈信息由 required_coverage 评价；NL／整题 COMMUNICATE 已取消 | [指标清单](metrics-inventory.md) |

本轮没有向正式运行新增全量检查、模型验收、自动修复或巡检流程。

## 4. 数据与发布状态

唯一活动清单是 [`data/tau2/benchmarks/gaia2.json`](../data/tau2/benchmarks/gaia2.json)。当前清单修订为 `gaia2-release-v3-reference-status`；这是版本说明更新，活动任务文件未切换到修复候选。

| 集合 | 数量 | 清单状态 | 评分归属 |
| --- | ---: | --- | --- |
| base | 800，五类各 160 | released | primary |
| mind_change | 100 | released | diagnostic |
| repetition | 100 | released | diagnostic |
| addtask／modify／cancel | 100／50／50 | draft | separate |
| hidden_intent | 100，独立集合 | draft | diagnostic |

主文件包含 1000 道题；另有 200 个交互变体，构造后共 1200 个运行视图。隐藏信息 100 道不计入这 1200 个视图。固定 async 题集和 trial5 检查集不属于当前活动范围。

参考修复候选为 `gaia2-reference-repair-simple-v1-candidate`，保留在原归档位置。1000 个主任务、200 个变体均与修复前输入逐题一致，没有合并冲突；200 个修复后变体均可构造，6 个代表题的离线参考执行与重放通过。

候选仍有 67 道 `unresolved`，没有执行全量参考封存，因此未激活、未改写旧 `verified` 或 runtime pin，也未把 draft 集合升级为 released。隐藏集继续绑定自己的旧源版本，不跟随候选自动改写。具体哈希、验证范围和样例见[参考修复收尾记录](gaia2-reference-finalization-report.md)。

## 5. 暂缓事项

- **错误、重试与统计口径**：后续单独整理错误分类、重试范围和聚合分母；本轮保留原计算与历史比较规则。
- **默认采集与指标事实依据**：后续明确默认采集项及各指标使用的在线状态、操作记录、重放状态、用户交付内容；本轮仅完成已经授权的公共证据边界修复。

## 6. 发布前尚未完成

- 在独立离线修复流程中处理或明确处置 67 道 unresolved，保留原 finding 与修复记录；不在评测运行中修复题目。
- 采用最终数据后，通过已有 reference verification／seal 流程封存对应数据与运行时，再切换活动来源及 pin。六个样例通过不能替代正式封存。
- GPT Live、Gemini Live 将来需要时，从统一 Adapter 层重新新增完整实现，不恢复本轮删除的零碎接入。

## 7. 权威文档索引

- 运行入口：[运行说明](running-the-benchmark.md)、[CLI 参数](cli-reference.md)、[Gaia2](gaia2.md)。
- 集合与版本：[活动 manifest](../data/tau2/benchmarks/gaia2.json)、[数据目录](gaia2-data-layout-plan.md)、[参考修复收尾记录](gaia2-reference-finalization-report.md)。
- 指标：[指标清单](metrics-inventory.md)。
- 接入：[Adapter 说明](../src/tau2/adapters/README.md)、[Qwen 说明](../src/tau2/adapters/qwen_audio_agent/README.md)。
- 模型角色：[API 配置](api-configuration.md)。

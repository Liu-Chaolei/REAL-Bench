# 指标精简实施计划

依据本轮讨论整理。目标是精简能力指标、统一按题汇总、将评测过程信息放入日志。实现依靠评分选择和数据流，不添加评测前后验收流程。

## 已确定修改

| 内容 | 处理 |
| --- | --- |
| 旧 ACTION、读写动作匹配统计 | 当前 Gaia2 评测取消，由工具、参数、顺序、效果四项诊断负责 |
| NL_ASSERTION 及其附带约束 | 取消计算和模型调用，不迁移约束到其他评分器 |
| 连续阶段 started/completed/missing_requests | 保留日志，取消因未交付直接清零整题奖励的关联 |
| operation_reward | 只留日志，保留现有操作检查和结果依赖 |
| persistent_task.reward | 结果分 × 回复分，不再独立乘 operation_reward |
| native_reward | 取消重复输出，统一使用 persistent_task.reward；native_result_reward 保留 |
| business_overlay_reward | 取消重复输出；修改/取消后的目标处理机制保留 |
| nonresponse、interruption、monologue | 取消粗粒度标记及其旧通话级总判定 |
| Late Rate、audible_pretoolspeech_rate | 取消计算和展示，保留其他指标需要的证据 |
| Generation quality、naturalness、speech_fidelity | 从当前评测调用和展示中退出，停止相关裁判调用 |
| complete_match、complete_match_rate、micro.f1 | 取消输出 |
| Precision、Recall、每题 F1、Macro F1 | 保留；同题多次试验先平均，再对题平均 |
| coverage、错误、成本、版本和证据状态 | 归入评测日志，不作为能力指标列展示 |

## 保留和暂缓

- selectivity、tool_misuse 保持现有定义。
- TICK/媒体版语音细项和 On Time Rate 阈值暂不统一；P90 等特殊统计口径随后处理。
- 后台操作关联、参数和时机判断保持现状。
- 不定义新的整题成功公式，不将四项诊断接入主奖励。
- 不定义新的不可评分与 Agent 失败边界，缺失值沿用现有处理。
- 发言有用性、Progressivity、Grounding、精简反馈、Hidden Ask Rate 保持定义。
- pass^k 保持组合公式 C(s,k)/C(n,k)。

## 实现步骤

1. 在评分入口排除当前 Gaia2 的 ACTION/NL 计算与结果组件。任务历史字段保留，不重建或重新封存数据。其他领域共用实现避免无关改动。
2. 停止 Generation quality 及两个相关 judge 的选择与执行，不采取先计算后隐藏的方式。
3. 简化后台评分输出；操作检查写入日志；消费者改读统一 reward。历史 native_reward 只在读取时兼容，不双写新结果。
4. 删除语音粗粒度标记、旧通话总判定、Late Rate 和工具前说话率的计算及展示。selectivity/tool_misuse 独立保留。
5. 精简 Intent 输出和两个汇总入口，保留 P/R/F1/Macro F1；匹配明细和覆盖信息留日志。
6. 对需要批量平均的能力分数按任务集、任务 ID 分组：试验内先计算，同题试验先平均，再对题平均。语音特殊统计暂缓。
7. 汇总结果区分能力值和评测日志。阶段交付只记录，不单独清零；其他终止规则不扩展。
8. 同步配置、CLI、报告、文档及受影响的现有缓存版本，避免旧结果冒充新口径。不新增发布审批体系。

## 轻量验证

- 只对改动文件做语法、格式及差异检查。
- 运行或调整直接涉及评分选择、后台公式、Intent 和汇总的少量测试。
- 用不同重复次数的两道题验证等权平均。
- 用小型本地证据检查结果与日志输出，通过 mock 确认取消的模型调用不会发生。
- 不运行完整 800/1100 题、不全量回放、不重新审核任务、不调用真实模型。
- 所有上述检查均为开发时验证，不加入评测系统的运行流程。

## 相关入口

- `src/tau2/evaluator/evaluator.py`、`outcome.py`：评分组件及结果合成。
- `src/tau2/evaluator/persistent_tasks/`、`src/tau2/metrics/active/persistent_task/`：后台评分。
- `src/tau2/metrics/call_quality.py`、`real_voice.py`：语音及通话指标。
- `src/tau2/diagnostics/intent/scoring.py`、`diagnostics/suite.py`：Intent 与批量汇总。
- `src/tau2/metrics/agent_metrics.py`、`src/tau2/scripts/`、`src/tau2/judges/`：展示、汇总和裁判入口。

## 实施记录

- 已实现 Gaia2 有效评分组件选择，历史任务文件保持原字节。
- 已精简后台评分、媒体语音指标、通话质量输出及 Intent 输出。
- 诊断汇总分为 metrics 与 evaluation_log，普通能力均值按题等权；语音样本汇总暂缓。
- 正常 judge suite 不再调度 naturalness/speech_fidelity。
- 旧字段的完整轨迹仍可留作历史日志，新质量 sidecar 使用 benchmark-quality-v2。
- 没有添加运行时验收步骤、没有运行全量题集或调用真实模型。

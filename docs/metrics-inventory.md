# 指标清单与精简后的口径

本文件记录当前实现。历史清理计划已完成；题目与历史轨迹没有重写。

## 0. 当前版本实际评测项

以下按当前 **Gaia2 全双工语音评测**整理，核对日期为 2026-11-26。以 [统一配置](../examples/real-metrics.unified.yaml) 和实际评分入口为准，不包含精简或新增指标的建议。

统一配置显式启用 5 个诊断模块：`intent`、`execution`、`voice`、`state_interaction`、`conciseness`。环境结果由任务评分入口提供。对应的常规指标为：

| 来源／模块 | 当前实际指标 | 评价内容 |
| --- | --- | --- |
| 环境评分 | `ENV_ASSERTION` | 环境目标是否达成；汇总字段为 `environment.env_assertion` |
| `intent` | 批量 `macro_f1` | 用户需求与实际后台任务之间的准确性、完整性；precision、recall 留在明细 |
| `execution` | `total` | 工具选择、参数、依赖顺序和执行效果的加权总分；四个分项留在明细，权重为 25/35/10/30 |
| `voice` | `latency`、`responsiveness`、`interrupt`、`selectivity` | τ-Voice 风格的四个语音交互聚合维度 |
| `state_interaction` | `progressivity.forward_rate`、`grounding.consistent_rate` | 是否推进相关工作，以及是否正确使用当时可见的状态与证据 |
| `conciseness` | `feedback_score` | 必要反馈覆盖与长度效率的 F3 综合分；两项留在明细 |

语音主报告显示四个聚合指标。`latency` 是 `response_latency_mean` 与 `yield_latency_mean` 的平均值；`responsiveness` 是 `response_rate` 与 `yield_rate` 的平均值；`interrupt` 使用事件级 `agent_interruption_event_rate`；`selectivity` 是三项 selectivity 的平均值。语音的完整 12 项计算如下，底层字段进入 `evaluation_log.metric_details.voice`：

| 类别 | 字段 |
| --- | --- |
| 响应（3） | `response_rate`、`response_latency_mean`、`response_latency_p90` |
| 让话（2） | `yield_rate`、`yield_latency_mean` |
| 主动打断（2） | `agent_interruption_event_rate`、`agent_interrupted_turn_rate` |
| 语音重叠（1） | `speech_overlap_duration` |
| 及时响应（1） | `on_time_rate` |
| 发言类型区分（3） | `selectivity_backchannel`、`selectivity_vocal_tic`、`selectivity_non_directed` |

另有两项依赖运行场景与证据，不是统一配置自动生成的普通诊断项：

| 场景 | 实际指标 | 评价内容 |
| --- | --- | --- |
| 后台请求管理／请求变化 | `persistent_task.completion_score` | 对应后台请求的结果与回复是否满足要求 |
| 隐藏信息 | `hidden_intent.hidden_ask_rate` | 隐藏信息节点中已询问节点的比例 |

现有 `avg_reward`、`pass^k` 结果统计仍保留。Intent 主报告只显示一个 Macro F1；precision、recall、执行四分项、反馈覆盖和长度效率仍保留在明细中。

实际能否得到数值仍受适用性和证据约束：Modify／Cancel 变体不评价 environment、execution、state_interaction；没有对应事件的语音分项为空；长度效率与反馈综合分需要对应语言的 calibration。不适用、缺证据与评分失败保留状态／原因，不补成 0 或 1。

## 1. 能力指标

| 家族 | 保留内容 | 当前计算口径 | 来源说明 |
| --- | --- | --- | --- |
| 任务结果 | ENV_ASSERTION、avg_reward、pass^k | Gaia2 单次只展示明确 ENV_ASSERTION；reward 仅作轨迹兼容字段，avg_reward/pass^k 暂留原计算；必要反馈信息由 required_coverage 评价，不自动进入 reward；平均奖励先同题平均，再对题平均；pass^k = C(s,k)/C(n,k) | τ-bench 框架及本地 Gaia2 适配 |
| 需求理解 | precision、recall、单次 f1、macro_f1 | P 为反向匹配比例，R 为正向匹配比例；F1=2PR/(P+R)；批量 P/R/F1 按题等权 | REAL-Bench 移植及本地适配 |
| 执行质量 | tools、parameters、order、effects | 单次定义及 25/35/10/30 权重保持现状；批量分项按题等权；总分按相同适用范围分组 | REAL-Bench/ARE 相关实现及本地适配 |
| 后台请求完成 | completion_score | 沿用原后台结果与回复的总判断；后台结果 passed、回复 passed/未评价及理由进入 evaluation_log，不单独作为性能分 | 本地扩展 |
| 统一语音交互 | response_latency_mean、response_latency_p90、response_rate、yield_latency_mean、yield_rate、agent_interruption_event_rate、agent_interrupted_turn_rate、speech_overlap_duration、on_time_rate、三项 selectivity | 正式发言分母；只有用户结束后的新 Agent 发言分组才是响应；持续旧发言不算 0 秒响应；1 秒静音确认让话，无及时期限；原始声音计算重叠；普通指标题目等权，P90 题目／试验等权加权分位数 | τ-voice、REAL-Bench/EVA 来源及本地统一定义 |
| 状态交互 | progressivity.forward_rate、grounding.consistent_rate | v2 逐事件可见状态；Agent 相关推进窗口率、实际状态使用机会一致率；同题试验平均后题目等权 | 通用状态管理与语义解释，本地实现 |
| 精简反馈 | required_coverage、length_efficiency、feedback_score | v2 只评价正确覆盖和长度，综合分为 F3；无独立真实性判断或重大错误清零，陈述真实性由 Grounding 评价；批量按题等权；中文初始预算 10+20N，英文初始预算 15+30N，按语言选择 calibration | 本地诊断实现 |
| 隐藏信息 | hidden_ask_rate | asked hidden nodes / hidden nodes；批量按题等权 | 本地隐藏信息扩展 |

“本地实现”描述代码来源层次，不证明指标思想为个人原创。外部来源清单见 `src/tau2/diagnostics/SOURCE.json`。

新记录统一输出 `voice_interaction` 的 12 项指标。事件打断率按 Agent 主动打断事件计数，轮次打断率按至少被主动打断一次的正式发言计数，两者分母都为正式发言数；事件率可大于 1。

On Time Rate 统计用户结束后新发言分组产生的已回应样本，取消 0.2 秒下限；所有请求统一上限 2.75 秒，不依赖工具调用或工具上下文。让话要求连续 1 秒静音确认，延迟记到实际停声点，无 2 秒及时期限；末尾无法确认的静音记录观察不足。

历史记录沿用原版本解释，缺少来源分类的轨迹不冒充新口径。实施记录见 [语音指标合并计划](voice-interaction-metrics-unification-plan.md)。

## 2. 取消项

- 当前 Gaia2 的 ACTION 独立评分、旧读写动作匹配率输出。
- 当前 Gaia2 的 NL_ASSERTION 评分与其附带约束检查；不迁移到别处，也不调用 NL 裁判。任务中的历史字段保留。
- 当前 Gaia2 的 COMMUNICATE 整题评分与对应专用裁判调用；后台回复判断保留为完成分明细，其他共用语义判断保留。
- 后台 native_result_reward、state_result_reward、reply_reward 与 reward 独立输出；新结果只保留 completion_score，旧字段读取时转为明细。
- tool_misuse 及正常 judge suite 的 tool_use 调度。
- 阶段未完整交付直接清零整题奖励的规则。
- 后台总公式中的独立 operation_reward 乘法项。
- native_reward 和 business_overlay_reward 重复输出；历史后台总分只作兼容读取。
- nonresponse、通话级 interruption、monologue 标记及旧五项合成 Interaction 判定。
- Late Rate、audible_pretoolspeech_rate。
- Generation quality 与 naturalness/speech_fidelity 的运行裁判调度及质量汇总。
- complete_match、complete_match_rate、micro.f1。

旧 ACTION/NL/COMMUNICATE 的其他领域共用评分器仍然存在。独立 judge suite、naturalness/fidelity/tool_use、语言专项 judge、候选目录和 authoring 已删除；历史结果文件保留。通话质量计算只读语音 ticks，不依赖裁判结果。

通话级 `selectivity`（三类错误合计超过 3 次）已删除，相关质量 sidecar 只保留调用身份和运行日志，没有空的性能块。语音新口径版本为 `tau-voice-interaction/4`；旧版本单独汇总，不重评分。

## 3. 评测日志

以下内容留在日志或明细，不进入能力面板：

- 阶段开始、结束、missing_requests；
- operation_reward、操作匹配、关联、时机和参数判断；
- coverage、状态、缺失原因、异常、结束原因；
- 成本、调用次数、耗时、重试和有效样本数；
- 输入、模型、版本、哈希和证据明细；
- 发言匹配、状态迁移、后台请求交付等过程记录。

分数的 lower/upper 仍可随能力值展示，它表示当前证据下的分数范围。未定义新的不可评分政策。

## 4. 输出与汇总

`tau2 metrics run/summarize` 默认打印能力指标。完整报告（包含日志）写在原有 `diagnostics/report.json` 或通过 `--output` 保存；`tau2 metrics export` 输出完整 JSON。

```json
{
  "version": "tau-real-diagnostics/2",
  "metrics": {"intent": {"macro_f1": 0.74}},
  "evaluation_log": {
    "total_trials": 10,
    "metric_details": {"intent": {"precision": 0.8, "recall": 0.7}},
    "records": [],
    "statuses": {}
  }
}
```

示例数字仅展示结构。Macro F1 是逐题 F1 的平均，不能从已平均的 P/R 重新计算。

普通能力均值先在同题多次试验内平均，再对题平均；按 domain、language、task_set_name、task_id 区分身份。同题跨运行分片仍合并。旧记录缺少身份字段时只能使用已有记录标识，不臆测任务身份。

信息覆盖使用现有 conciseness 流程；DeepSeek all 配置已包含英文 calibration。没有对应语言 calibration 时只输出覆盖和原始长度，长度效率及综合分保持空值，不回退到其他语言预算。

新语音均值和比例按同题试验平均、再对题等权；P90 按题等权分配总权重，再分配到试验及响应样本，取累计权重首次达到 0.9 的延迟。不同配置、不同 Intent 规则版本仍分组报告。

## 5. 暂缓事项

- 新的整题成功定义与综合总分；
- 评测无效和 Agent 失败的边界；
- 三项 selectivity 细分指标保留；通话级错误次数阈值标记已删除。
- Progressivity、Grounding、精简反馈之间的取舍；发言有用性已从当前统一配置移除。

本次验证使用离线小用例和 mock，不向评测运行中添加验收流程，不重新生成或封存数据，不调用真实模型。


状态交互 v2 已接入同次诊断报告，需在 measures 中显式选择 `state_interaction`。语义解释使用该角色的模型配置和共享请求预算；不再隐式创建另一配置评分。发言有用性不再作为当前统一配置的评测项；历史 interaction 结果仍可读取。实现说明见 [状态交互实施记录](state-interaction-implementation-plan.md)。

# Task 0005：conciseness 异常记录与 fallback 建议

记录日期：2026-11-20。本文记录已确认的错误和待实现的方案；本次仅写文档，没有实施下述 fallback，也没有修改原运行结果。

## 运行与影响

- Task：`0005_nlme4nanrpyu7ny7c55biii97somt6x8`。
- Run：`realtime-dynamic-metrics-task0005-20261119-retry`。
- Simulation：`8c843009-cf72-4ad0-b461-e0ced5cd625f`。
- 前端 Qwen Realtime，dynamic 启用，user/judge 使用 `openai/deepseek-flash`，启用五项独立 metrics。
- 对话耗时约 562 秒，三阶段完成，以 `user_stop` 结束；任务 reward、ENV、NL、communication 均为 1.0，execution 为 100 分。
- diagnostics evidence 收集成功。此前 `BackendInput.received_monotonic` 导致整包证据封存失败的问题已修复，和本文的错误不同。
- 本次 conciseness 三个窗口均未进入有效评分，汇总为 `evidence_incomplete`，`scored_windows=0/3`，分数为 `null`，不是零分。

证据入口：[运行结果][run]、[完整原始 evidence][evidence]、[conciseness 汇总][summary]。

## 当前评分流程

1. 按实际送达的 stage 划分窗口，收集用户请求和当时的执行证据。
2. 第一轮 LLM 根据这些证据生成 nugget inventory，即“本阶段应向用户反馈的信息点”。不提供当前待评分的 agent 回复，不用 GT/reward 充当答案。
3. Python 检查 inventory 的结构、来源引用和完整性；只有合法且 `status=complete` 的清单才冻结。
4. 第二轮 LLM 将实际 agent 回复与冻结清单逐点对齐，Python 计算覆盖率、长度效率等指标。

本次 stage 1、2 均失败在第 3 步，尚未进入回复对齐；不是 agent 因这些异常被判业务失败。

实现见 [evaluator.py](../src/tau2/diagnostics/conciseness/evaluator.py)、[models.py](../src/tau2/diagnostics/conciseness/models.py)、[证据投影](../src/tau2/diagnostics/conciseness/evidence.py)。指标口径见 [精简反馈评测](concise-feedback.md)。

## Stage 1：ValueError

**用户要求**：删除已收藏 Prague 房源中第二贵的一个，保留最贵的，确认最贵房源仍收藏，并告知剩余 Prague 房源数量。用户随后批准删除 `Prague Palace`。

**实际执行**：成功删除 `Prague Palace`；完整的后续查询结果中还剩 3 个 Prague 房源，包含 `Prague 6 Luxury`。

**LLM 输出**：返回 `status=complete`、`outcome=partial_success`，共 4 个 nuggets。其中可选的 `n4` 表示：

> 当前结果中至少能看到三个仍收藏的 Prague 房源：Prague 6 Luxury、Pražský Palác、Prague 7。

但 `n4.evidence_refs` 只有工具来源：

```json
[
  "operation:7b7714a7-2c6e-4783-9698-1013a58c6d96"
]
```

缺少当前用户要求来源，例如 `user:caller:1198`。`validate_inventory()` 要求每个 nugget 至少引用一个 user source，业务结果还必须引用 operation source，因此本地重放原始响应时得到：

```text
ValueError: Nugget must cite a user requirement
```

**合理性**：拒绝这条引用不完整的清单符合现有规则。不能随意补一个 user ID 就声称语义关系已核实。不过，一个可选 nugget 的问题让整个窗口直接成为 `grader_error`，没有修复重试，恢复能力不足。

**还有一个被该异常遮住的问题**：LLM 的 `n3` 把“无法确认剩余总数”设为必答 limitation，却仍将整个清单标为 `complete`。这是 judge 输入截断造成的知识缺口，不能直接变成要求 agent 向用户承认不知道总数的新义务。因此只补 `n4` 引用或删除 `n4`，仍不能证明该清单适合正式评分。

文件：[窗口与来源][s1-window]、[生成输入][s1-input]、[原始 judge 响应][s1-attempt]、[错误记录][s1-error]。

## Stage 2：ValidationError

**用户要求**：新增收藏最便宜的、尚未收藏的 Prague 房源，要求 furnished、允许宠物、月租不超过 2500；保持其他收藏不变，反馈新增房源名称、月租和最终 Prague 收藏总数。

**实际执行**：收藏 `Pražská Perla`，月租 1800，最终 Prague 收藏数为 4。

**LLM 输出**：认为用于确认总数和最低价的列表不完整，返回 `status=unavailable`、`outcome=partial_success`，但同时输出了 4 个 nuggets，混合了成功结果和无法验证的事项：

```json
{
  "status": "unavailable",
  "outcome": "partial_success",
  "nuggets": [
    {"id": "n1", "proposition": "Pražská Perla 已收藏，月租 1800"},
    {"id": "n2", "proposition": "无法确认最终 Prague 收藏总数"},
    {"id": "n3", "proposition": "无法确认它是符合条件的最便宜房源"},
    {"id": "n4", "proposition": "该房源位于 Prague、已配家具、允许宠物"}
  ]
}
```

上面省略了各 nugget 的其他字段，仅示意状态与清单的冲突。`NuggetInventory.complete_inventory()` 规定 `status != complete` 时，评分清单必须为空。本地重放原始响应得到：

```text
ValidationError: 1 validation error for NuggetInventory
Value error, Incomplete inventories must not contain scored nuggets
```

**合理性**：在现有 schema 下拒绝该响应是正确的；不可用状态不能携带会进入正式计分的半份清单。保守表示应是 `status=unavailable`、`outcome=evidence_incomplete`、`nuggets=[]`。原响应可以单独保存用于审计。

该响应的可选 `n4` 也缺少 user 来源，但程序先在 inventory 级校验失败，尚未检查到这里。不能仅将 `status` 改为 `complete` 来绕过错误。

文件：[窗口与来源][s2-window]、[生成输入][s2-input]、[原始 judge 响应][s2-attempt]、[错误记录][s2-error]。

## 共同上游原因：judge 输入被截断

`FeedbackConfig.evidence_max_characters` 默认是 **12000**。`project_value()` 对超过此上限的单个工具结果或状态字段，只保留 JSON 字符串前缀，并标记 `omitted=true`。这与运行配置的整体 prompt 上限 `max_characters=200000` 是两回事。

本次完整数据仍保存在原始 evidence 中，但构造 conciseness 输入时发生了如下截断：

| 工具结果 | 原始列表条目数 | 与任务有关的完整事实 | 传给 conciseness judge 的情况 |
| --- | ---: | --- | --- |
| 首次 `RentAFlat__list_saved_apartments` | 30 | 4 个 Prague 收藏；最高价 3000，第二高价 2800 | 结果长 12729 字符，被截断 |
| 删除后的 `RentAFlat__list_saved_apartments` | 29 | 3 个 Prague 收藏；最贵房源仍保留 | 被截断，无法用预览证明精确总数 |
| `RentAFlat__list_all_apartments` | 125 | 提供筛选最便宜合格房源所需的完整候选 | 被截断，无法用预览证明全局最小值 |
| 新增后的 `RentAFlat__list_saved_apartments` | 30 | 4 个 Prague 收藏，包含月租 1800 的 Pražská Perla | 结果长 12716 字符，被截断 |

Stage 0 的 judge 正确返回了 `unavailable` 且 `nuggets=[]`，理由也是无法从预览排除同价房源。Stage 1、2 面临类似信息缺口，但输出没有完整遵循契约。

结论：原始证据并没有丢失。当前以字符前缀控制输入长度的方式，会主动丢掉计数、排序、全局筛选所需的完整性。judge 拒绝据此猜答案是合理的；把这种后处理知识缺口算作 agent 的业务错误不合理。

## 当前恢复与记录的不足

- [TauJsonModel](../src/tau2/diagnostics/model.py) 对 timeout 已有一次重试；本次两个输出校验异常不属于 timeout，不会自动重试。
- 非法原始响应已保存在 `conciseness-cache/requests/attempts/`，所以能离线复现具体错误。每个窗口的 `error.json` 只记录异常类型，未写出具体原因、nugget ID 或失败步骤。
- 一个窗口异常后继续下个窗口，但该窗口不再建立清单或做 alignment；缺失窗口不能被静默当成没有义务。
- 本次配置 `conciseness: {}`，没有适用于英语的长度预算校准。即使修复以上异常，也不能保证得到单一综合分；信息覆盖、长度、真实性等仍可独立报告。

## 可以增加的 fallback（尚未实施）

建议按以下顺序处理，而不是一律忽略校验错误：

### 1. 先恢复已采集的必要证据

- 优先从封存的完整 operation result 重新构造有界输入，而不是对同一份残缺预览反复调用 LLM。
- 对计数、排序、最便宜等要求，应提供完整的相关候选与必要字段，并注明过滤范围、条目数和来源；可以分块处理，但不能把部分列表伪装成完整列表。
- 通用方案可先完整提供必要工具结果、压缩无关字段；仅提高 12000 上限可作为小实验，不能保证所有任务都覆盖，也受整体 prompt/token 上限约束。
- 只有确实无法补齐与该要求相关的证据，才保留 `unavailable/evidence_incomplete`。不能看到任何截断就跳过整个窗口，因为被截掉的内容也可能与要求无关。
- 不向 nugget 生成器补 GT、隐藏答案、任务 reward 或当前待评 agent 回复；只使用该窗口允许使用的真实采集证据。

### 2. 输出校验失败后做一次定向 repair

- 保留原始输入、schema、非法响应和结构化错误；给同一 judge 一次修复机会，计入调用预算和超时。
- Stage 1：明确指出 `n4.evidence_refs` 缺少用户来源，让模型核实关联后修正或删除该可选项，同时遵守完整性规则。
- Stage 2：指出 `unavailable` 必须配空评分清单；不要要求模型为了能计分而改成 `complete`。
- 只修复格式、引用和契约冲突，不反复重抽已合法冻结的标准来挑高分；inventory repair 仍不看当前 agent 回复。

### 3. 无法修复时保守降级

| 情形 | 可选 fallback | 限制 |
| --- | --- | --- |
| `unavailable/not_applicable` 却携带 nuggets | 清空正式 `nuggets`，原清单保存为独立审计附件，标记归一化原因 | 不把被清空的部分当成已评分；不自动判业务成功/失败 |
| 个别 optional nugget 引用非法 | 可考虑丢弃该可选项并记录 `dropped_nuggets` | 必须确认 required 清单完整，且不以删除可选项改善正式分数；口径改变需版本记录 |
| required nugget 非法或完整性无法证明 | 保留窗口，不给确定语义分，明确标记不可用原因 | 不能删除必答点以缩小分母，也不能把 judge 的证据缺口变成 agent 必须表达的 limitation |
| 完整 inventory 已冻结，但 alignment 无法修复 | 对未能判断的点保留为 `uncertain`，保留分母与覆盖率/分数上下界 | 未核实的结论不给信用，未知不能当作已匹配或遗漏 |
| 本地代码异常、缓存损坏、协议异常 | 保留 `grader_error` 并记录详情 | 不要统一改名为 `evidence_incomplete`，否则会掩盖程序问题 |

仍可输出实际回复长度、语音时长、播放时长，以及有明确范围的局部观察；这些不能冒充完整窗口的正式 conciseness 分数。

### 4. 错误和缓存可审计

- 错误记录增加 `step`、异常类型、简短 message、nugget/字段路径、attempt 次数、fallback 动作和原始响应文件路径；避免保存凭据。
- 区分“证据不足”“judge 输出校验失败”“provider 超时”“程序错误”，分别统计窗口覆盖率。
- 不完整窗口保留在 trial 分母/覆盖率报告中，不只平均幸存的可评分窗口。
- 只有通过语义与来源校验的完整标准才能冻结；修复前后的输入、规则版本及缓存身份要区分，历史结果不得静默覆盖。

## 后续最小验证建议

1. 用上述两份原始响应离线重放 validator，确认仍能定位精确错误；无需调用 LLM。
2. 用完整 evidence 构造候选输入，验证 Prague 收藏数为删除前 4、删除后 3、新增后 4，并检查最低价筛选的数据范围完整。
3. 用一次 repair 测试验证：引用错误能够被定位，`unavailable + nuggets` 不再导致整个窗口只有异常类型；不要求不可用窗口得到分数。
4. 验证 required 缺失仍无完整分，optional 丢弃有记录，alignment 的未知点不从分母中消失，合法冻结清单不被重新抽取。
5. 单独验证英语 calibration 缺失时仍明确输出 `uncalibrated_language` 和 `null`，避免把“恢复清单生成”和“已能计算综合分”混为一谈。

[run]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/results.json
[evidence]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evidence/evidence.json
[summary]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness.json
[s1-window]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_1/window.json
[s1-input]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_1/generation-input.json
[s1-attempt]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/conciseness-cache/requests/attempts/e4a0b562d107ea2615d0fa9fb4a7a6de61808b52da684f953708c82bc6914f2f_a3a2c12044194a46b5d80adaa3fe4f5a.json
[s1-error]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_1/error.json
[s2-window]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_2/window.json
[s2-input]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_2/generation-input.json
[s2-attempt]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/conciseness-cache/requests/attempts/8bd78bc666aa0dac8005d6f2136f1349d464f5dc684b46614a72d1e517e37a0e_d50cdc35d04b4543ad230af29618c82d.json
[s2-error]: ../data/simulations/realtime-dynamic-metrics-task0005-20261119-retry/artifacts/task_0005_nlme4nanrpyu7ny7c55biii97somt6x8/sim_8c843009-cf72-4ad0-b461-e0ced5cd625f/diagnostics/evaluations/0f2e809e01b2de0358582539f41ddc1bd39f5b75c404698c28f2a88415501f09/conciseness/stage_2/error.json

# 英文信息预算接入计划

状态：已实施。本文记录英文 `conciseness` 长度预算的接入方案，不修改任务数据、不改变 Agent 运行过程、不改变主任务 reward。

## 1. 目标

为英文反馈增加一套独立的信息长度预算，并把它接入现有的离线 `conciseness` 诊断流程。

英文和中文使用相同的信息点定义、匹配方式和长度计算方式，只使用不同的预算参数。

预算只影响反馈长度相关的诊断结果，不进入：

- Agent 运行过程；
- Gaia2 的 ENV Assertion；
- 需求理解指标；
- 四项执行质量指标；
- 后台任务分数；
- 语音交互指标；
- 任何新的运行时验收或检查流程。

## 2. 已确认的英文初始预算

英文先使用下面的初始参数：

```text
base = 15
per_nugget = 30
optional_cap = 0.25
```

预算公式为：

```text
B = 15 + 30 × (
    required_matched
    + min(optional_matched, 0.25 × required_matched)
)
```

这里：

- `required_matched`：正确覆盖的必要信息组数；
- `optional_matched`：正确覆盖的可选信息组数；
- `base`：每个反馈窗口的基础字符预算；
- `per_nugget`：每个已覆盖必要信息点增加的字符预算；
- `optional_cap`：可选信息对预算增加的上限。

这组参数先作为英文初始版本使用。后续可以收集英文窗口并进行人工复核、拟合和留出验证，再发布新的校准版本。

## 3. 信息点怎么定义

中英文不重新设计两套信息点规则。两种语言都遵循同一套业务定义：

- 一条可以独立核对的业务要求算一个信息点；
- 对象、数值、单位、条件和时间范围必须保留在同一个信息点中；
- 同义表达放在同一个 `equivalence_group`；
- 不把一句话、一个 JSON 字段或一个关键词机械地当作信息点；
- 用户必须知道的信息标记为 `required`；
- 直接有帮助但不是必要的信息标记为 `optional`。

例如：

```text
Tell me whether the refund was approved, the refund amount,
and when it will arrive.
```

可以拆成三项：

1. 退款是否获批；
2. 退款金额；
3. 到账时间。

不能把“退款”“金额”“时间”拆成三个没有完整业务含义的碎片。

信息点生成仍然使用现有的两阶段机制：第一阶段只看用户请求、修改和执行证据，不提供待评价的 Agent 回复；第二阶段用冻结的信息点清单匹配实际回复。

## 4. 英文校准文件

新增文件：

```text
examples/calibration/concise-feedback.en-v1.json
```

结构如下：

```json
{
  "calibration": {
    "id": "en-feedback-initial-v1",
    "version": "tau-feedback-calibration/1",
    "length_rule": "nfc_nonwhitespace/1",
    "reviewed": true,
    "provenance": "Initial English budget selected from reviewed examples; corpus calibration and held-out validation remain pending.",
    "review_sha256": "<sha256 of the calibration source>",
    "languages": {
      "en": {
        "base": 15,
        "per_nugget": 30,
        "optional_cap": 0.25
      }
    }
  }
}
```

`review_sha256` 必须来自实际校准来源文件，不能手写占位值后用于正式评分。

`reviewed: true` 说明当前参数已经被确认采用，不表示已经完成大规模英文人工标注或留出集验证。后续校准应发布新的 `id`，不要覆盖历史预算的含义。

如果需要在同一个配置里支持多种语言，可以使用同一个 calibration 文件：

```json
"languages": {
  "en": {"base": 15, "per_nugget": 30, "optional_cap": 0.25},
  "zh": {"base": 10, "per_nugget": 20, "optional_cap": 0.25}
}
```

## 5. 接入实际诊断过程

### 5.1 根据运行语言选择预算

`conciseness` 诊断从已保存的证据语言读取配置：

1. 优先使用显式 `conciseness.language`；
2. 否则使用 `evidence.source.language` 或保存的语言上下文；
3. 在 calibration 的 `languages` 中查找对应参数；
4. 找不到对应语言时保持未校准状态。

不把英文自动套用中文预算，也不在任务文件中保存预算。

### 5.2 每个反馈窗口独立计算

多阶段任务按已记录的请求阶段建立窗口。每个窗口独立处理：

1. 生成必要／可选信息点清单；
2. 冻结信息点清单；
3. 匹配 Agent 实际回复；
4. 统计覆盖情况；
5. 计算回复长度；
6. 根据该窗口的 `M` 和 `O` 计算长度预算；
7. 计算 `length_efficiency`。

一个窗口没有用完的预算不能转给另一个窗口。窗口内拆成多条 Agent 回复时，长度累加。

### 5.3 长度计算规则

继续使用当前统一长度规则：

- 先做 NFC 规范化；
- 统计非空白 Unicode 字符；
- 空格、换行等空白不计；
- 字母、数字和标点计入；
- 不按 Token 数、单词数或语音秒数计算；
- 重复内容和填充语会增加实际长度。

长度效率为：

```text
length_efficiency = min(1, length_budget / response_length)
```

没有正确覆盖任何必要信息，效率为 0；回复为空时也为 0。

回复短于预算时效率封顶为 1，不继续奖励更短的表达。

## 6. 哪些结果受英文预算影响

英文预算只影响以下结果：

- `length_budget`；
- `length_efficiency`；
- `raw_feedback_score`；
- `feedback_score`。

这些结果仍与 `required_coverage` 一起解释：覆盖率看必要信息有没有说全，长度效率看表达是否超过合理预算。

漏掉必要信息时，预算只按照已经正确覆盖的信息点计算。因此短但不完整的回复可能长度效率很高，同时必要信息覆盖率很低。

事实错误由现有 `fidelity_status` 和 `feedback_score` 规则处理。业务任务失败本身不自动让长度效率变为 0，只要 Agent 准确说明实际失败情况，仍可以有合理的长度效率。

## 7. 未校准和不确定情况

没有适用语言预算时：

- 仍计算信息点和 `required_coverage`；
- 仍记录 `response_length`；
- `length_budget`、`length_efficiency`、`raw_feedback_score` 和 `feedback_score` 返回 `null`；
- 记录 `uncalibrated_language` 或具体不可用原因。

如果信息点或匹配结果存在不确定性：

- 保留 `required_coverage_lower` 和 `required_coverage_upper`；
- 保留长度效率及综合分的上下界；
- 正式确定值保持 `null`。

如果窗口没有必要的用户反馈要求，标记为不适用，不自动给长度效率 1。

## 8. 配置文件接入

建议的配置方式：

```yaml
measures:
  - intent
  - execution
  - voice
  - interaction
  - state_interaction
  - conciseness

conciseness:
  calibration: calibration/concise-feedback.en-v1.json
```

英文专用配置可以直接指定英文文件。多语言配置应使用包含多个语言的 calibration 文件，由运行语言自动选择。

`examples/concise-feedback.yaml` 更新为支持按保存语言选择预算。用于英文主评测的诊断配置（例如 `examples/real-metrics.deepseek-all.yaml`）显式加入英文 calibration 路径；不在基础任务数据中写入预算。

## 9. 版本、缓存和结果记录

校准信息必须进入诊断 profile 和缓存哈希：

```text
profile = {
  metrics_config,
  calibration_id,
  calibration_version,
  calibration_file_hash,
  implementation_digest
}
```

修改预算或评分代码后不能复用旧结果。中文和英文结果也不能混用同一份预算身份。

每个结果记录必要的来源信息：

```json
{
  "language": "en",
  "calibration_id": "en-feedback-initial-v1",
  "length_rule": "nfc_nonwhitespace/1",
  "budget": {
    "base": 15,
    "per_nugget": 30,
    "optional_cap": 0.25
  }
}
```

这些属于可复现日志，不新增 Agent 性能指标。

## 10. 汇总方式

1. 每个反馈窗口独立计算；
2. 一次试验内，对适用窗口等权平均；
3. 同题多次试验先平均；
4. 再对题目等权平均；
5. 没有可评分窗口的试验保留状态，不伪造 0 或 1。

不同语言、不同校准版本和不同评分规则版本分开汇总。

## 11. 正式校准的后续流程

当前 `15 + 30N` 是英文初始预算。后续如果要升级为正式校准版本：

1. 收集约 40～60 个真实英文请求窗口；
2. 覆盖简单查询、工具成功、部分成功、失败、未知、澄清和不同长度；
3. 两位人工评审分别确认必要／可选信息点和自然、完整、简洁的参考回复；
4. 解决信息点数量和参考回复的分歧；
5. 仅在 development 数据上拟合预算；
6. 使用 validation 数据报告预算覆盖率；
7. 发布新的 calibration ID 和来源哈希。

这一步不进入每次评测运行，也不需要实时检查。

## 12. 轻量验证

只做离线机制测试：

- 英文运行选择 `en` 参数；
- 中文运行继续选择中文参数；
- 三个必要信息点得到 `15 + 30×3 = 105` 个字符预算；
- 可选信息只按 `0.25 × required_matched` 增加预算；
- 超过预算时效率按预算／实际长度计算；
- 没有英文校准时长度结果为 `null`，不退回中文预算；
- 修改校准文件后 profile 哈希变化，旧缓存不复用；
- 同一窗口拆成多条回复时长度正确累加。

不执行完整数据集回放，不调用真实模型，不增加运行前验收、运行中检查、运行后审计或双版本评分。

## 13. 实施记录

已完成：

1. 新增 `examples/calibration/concise-feedback.en-v1.json`，写入 `15 / 30 / 0.25`；
2. 新增 `examples/concise-feedback.en.yaml`；
3. `examples/real-metrics.deepseek-all.yaml` 接入英文 calibration；
4. 配置加载时记录 calibration 原始文件 SHA-256，并在结果中保存 `calibration_source_sha256`；
5. 更新 `docs/concise-feedback.md` 和指标清单；
6. 用本地配置加载和公式样例确认英文预算、中文预算与三信息点预算 105 字符均正常。

后续如要升级为正式统计校准，再按照人工复核流程发布新的 calibration ID，不覆盖当前初始版本。

验证记录：英文和中文示例配置均能加载各自预算；3 个必要信息点和 1 个可选信息点时，英文预算为 127.5 个非空白字符，中文预算为 85 个非空白字符；calibration 原始文件哈希会写入诊断结果。现有 conciseness 测试中有 2 项仍断言工作区旧版的顶层汇总结构（`report["conciseness"]`），当前实现已使用统一的 `report["metrics"]["conciseness"]` 结构；这些失败与英文预算无关。

完成后，英文预算只作为评测后的反馈长度诊断参数，不改变任务执行、主 reward、其他指标或评测运行机制。

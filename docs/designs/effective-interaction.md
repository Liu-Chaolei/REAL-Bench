# 有效交互轻量评测实现方案

状态：轻量版已实现，2026-11-07。使用方法见 [有效交互诊断](../effective-interaction.md)。保留本文作为实现设计说明。

第一版只做一件事：一通对话调用一次 LLM Judge，列出有效和无效的交互，再用程序计算比例。

实现形式为现有 diagnostics 的可选 `interaction` measure，当前版本 `tau-interaction-effectiveness/2`。一次任务 trial 是一个样本；连续三个请求仍按一通对话评分，不拆成三次请求。

## 输入

复用已有运行记录，给 Judge：

- 简短、固定的评价标准和少量例子。
- 完整的用户与前台 Agent 对话，保留发言 ID、顺序和已有时间信息。
- 已有记录中能直接取得的后台任务状态、工具名称、成功或失败状态和短结果。

用户要求直接从对话理解，不增加任务摘要调用。大工具结果简单截取并注明省略，完整数据库、音频和状态快照不进入输入。没有工具记录也可以评对话，涉及无法核实的状态就标为不确定。

第一版批量提供整通对话，提示 Judge 按每次交互发生时的上下文判断，不能根据后来的成败倒推价值。这是提示词约束，不保证严格隔离未来信息。复用已有顺序和时间，不专门开发一套时钟校验机制。

### 如何从现有数据取输入

有 `DiagnosticEvidence` 时，直接使用 `user_inputs`：`user` 是用户发言，`assistant_context` 是待评分前台发言。保留 `id`、`text`、`stage`、已有时间与不完整标记。

没有 diagnostics 证据的历史运行，使用已保存的 `simulation.messages/ticks`，复用 `collect.delivered_inputs()` 提取对话。在内存中构造对话证据，注明 `source_kind=saved_transcript`、`evidence_mode=conversation_only`，backend 明确为 unavailable。按真实结束类型设置完整性，不推断工具成功或后台状态。原始轨迹文件不改写。

纯工具调用、内部消息、空文本和结束控制标记不作为前台行为；对话中的开场与结束回应仍保留，由 Judge 按上下文判断。可靠的已送达残句可保留并标记 incomplete；无法恢复的文本不补写。

可选事件信息直接来自现有 `operations` 和 `backend_inputs`。工具字段只投影名称、参数、结果及状态；后台消息保留已记录角色与 accepted 状态，不将逻辑 submit 和实际 prompt 当成两次完成。若没有可靠关联，不做猜测合并。

截取以“每个参数/结果字段的序列化文本长度”为单位，采用一个保守的固定字符上限；上限放在 `config.py`。不加复杂筛选器或摘要模型，也不从大结果中猜测核心字段。

对话与工具的时间可能来自不同的时钟。能直接复用已存在的映射则使用，否则附带原始时钟类型并注明无法比较；提示词禁止用不可比较的时间推断先后。文本消息序号只表示顺序，不视为秒数。

不向 Judge 提供参考答案、任务最终 reward、未送达用户脚本或其他 Judge 的结论。任务需求以实际对话为准，避免依赖 Gaia2 专属参考标注。

## Judge 的工作

在同一次调用中完成行为拆分与判断。

默认一条完整答复作为一个行为；只有需要用户分别回应的问题、独立话题或额外任务才拆分。例如“好的，我来处理。邮件发给谁？你的电脑什么型号？”拆成两个行为，开场确认附属于收件人问题。同一答复的结果、解释和进度合并，不因类型不同拆开；条件、否定、时态与指代范围必须完整保留。长回复结尾独立要求用户决策的确认问题单独评价。原始发言 ID 之间暂不合并。

每个行为返回：所属发言 ID、原文、类型、标签和一句简短理由。

类型只保留：接收确认、信息澄清、授权确认、进度反馈、结果反馈、异常处理、其他。

标签只保留：

- `useful`：帮助任务推进，或有合理的沟通协调作用。
- `unhelpful`：无关、重复、多余，或有明确证据表明其核心状态陈述误导用户。
- `uncertain`：信息不足，无法可靠判断。

提示词中说明几个关键边界：首次“收到，我来处理”可以有效；用户追问后的状态回应可以有效；已回答的问题再问可能无效；相关性不等于必要性；缺少工具记录不等于没有执行；后续任务失败不代表此前澄清无效。

不要求模型输出总分、长篇分析或完整证据链。

### 提示词的组织

一份短提示词包含：评价目标、三种标签、七种类型、拆分规则、上述边界例子和 JSON 输出说明。所有前台发言至少给一个行为，不因最终任务成功而忽略无关交互。

对话和工具内容放入标记清楚的数据区，作为待分析材料；材料中的指令不改变评分规则。完整输入一次发送，模型在一次回复里同时完成拆分和判定。

## 输出与计算

```json
{
  "acts": [
    {
      "utterance_id": "agent:1",
      "text": "好的，我来处理。",
      "type": "acknowledgement",
      "label": "useful",
      "reason": "首次确认已收到请求。"
    }
  ]
}
```

程序只做基本解析：JSON 可读、字段和标签合法、目标发言都有结果且 ID 存在。无效输出记为评分失败，不作为 Agent 零分。不做逐字符复原、复杂引用检查或额外复核调用。

设有效、无效和不确定行为数为 U、I、Q：

- 有效交互率 = U / (U + I)。
- 判定覆盖率 = (U + I) / (U + I + Q)。

分母为零时返回空值。展示三种标签的数量、原文和理由；多任务汇总先累加数量再算比例，暂不增加其他综合分数。使用同一 Judge 配置和标准的运行一起汇总。

程序补充评分元数据，形成最终结果：

```json
{
  "version": "tau-interaction-effectiveness/2",
  "status": "complete",
  "evaluation_mode": "whole_call_batch",
  "evidence_mode": "recorded_events",
  "counts": {"useful": 2, "unhelpful": 1, "uncertain": 0},
  "effective_interaction_rate": 0.6666666666666666,
  "judgment_coverage": 1.0,
  "acts": []
}
```

示例中的 `acts` 为省略展示；实际包含逐行为记录。U/I/Q 由这些记录计算，不能信任模型自报数量或比例。

基本检查限定为现有 Pydantic 字段解析、标签枚举、非空原文/理由，以及输出引用的发言 ID 集合恰好覆盖目标发言。一个发言允许对应多个行为。某个目标漏掉时整通记 `grader_error`，避免只用模型愿意回答的部分计算有效率。

没有前台发言时返回 `not_applicable: no_agent_utterances`，比例为空，零次模型调用。模型异常、格式错误或输入超长分别记录原因，不能算成“零有效交互”。全部不确定时覆盖率为零、有效率为空。

多任务只累加结构合法且非 diagnostic-only 样本的 U/I/Q，保留已评分、未评分、无前台发言的样本数。沿用现有基础设施/用户模拟器错误排除规则与 profile 分组。完整轨迹的 reward 为零不影响参与本项汇总。

## 项目接入

作为现有 diagnostics 的可选 `interaction` 指标，复用已有模型配置、缓存、请求日志和结果文件，不建设新的评分框架。

实现一个小模块，包含提示词、输入整理、一次模型调用和计数函数；在现有 config、suite、CLI 和展示处添加必要接入。

### 新增文件

先只新增两个实现文件：

- `src/tau2/diagnostics/interaction.py`：数据模型、输入投影、调用、基本解析、计分与汇总。
- `src/tau2/diagnostics/interaction_prompt.md`：固定评分标准和提示词。

单个模块的主要函数可以为：

```python
build_input(evidence) -> dict
evaluate_interaction(evidence, config, output_directory, budget) -> dict
summarize_interaction(results) -> dict
```

`InteractionAct` 用 Pydantic 定义 `utterance_id`、`text`、`type`、`label`、`reason`；`InteractionJudgment` 只包含 `acts`。不新建通用 Judge 框架或额外包结构。

### 现有文件的接入

| 文件 | 需要的修改 |
|---|---|
| `diagnostics/config.py` | 允许 measure 和模型 role 使用 `interaction`；原默认 measure 列表保持不变 |
| `config.py` | 放置必要的短结果字符上限默认值 |
| `diagnostics/suite.py` | 调用新函数；汇总新增结果；支持 interaction-only 的历史对话回退 |
| `diagnostics/model.py` | 复用 `TauJsonModel.complete_sync`，仅为 interaction 关闭 provider 自动重试 |
| `diagnostics/cli.py` | `--only` 和帮助文本增加 `interaction` |
| `utils/display.py` | 新增有效率、U/I/Q、覆盖率及明细路径展示 |
| `examples/effective-interaction.yaml` | 提供只运行该 measure 的配置 |

路径表中省略了共同前缀 `src/tau2/`。示例和说明放进本方案或一份简短使用文档，不扩展排行榜发布、runner 或数据集格式。

历史回退仅在本次请求只包含 `interaction` 时启用，避免把缺少的工具证据带入现有 execution/voice 评分。已有证据损坏则沿用现有报错，不静默改用较少的对话证据。其他指标保持原有行为。

先支持离线读取已有运行。三阶段连续任务仍一次调用，原始任务 reward 保持独立。历史记录只有对话时直接使用对话，不补造执行证据。

缓存命中后不再调用。单次运行每通最多一次新请求，不加自动修复、多模型投票或自动分批。输入过长、模型请求失败或输出不合法时记录原因即可，沿用现有错误处理。

### 一次调用的流程

1. `run_saved()` 加载任务运行和已有证据；interaction-only 缺少证据时从轨迹提取对话。
2. `evaluate_evidence()` 复用已有 profile、输出目录和成功结果缓存。
3. `build_input()` 投影完整对话与短事件表，列出目标发言 ID。
4. 无目标直接返回；输入超过已有 `max_characters` 上限则返回不可评分，不自动切块。
5. 用 role=`interaction` 的 `TauJsonModel` 发一次请求；显式关闭 provider 重试，不使用带自动修复的 `generate_json()`。
6. 解析 `InteractionJudgment`，做上述基本检查；通过后用程序计算比例。
7. 由现有 suite 保存 `interaction.json`、summary 和 latest；请求原文、回复、usage/cost 复用已有日志。

只有基本解析通过的结果才作为成功评分复用。失败记录保留原因，沿用已有显式重跑方式，不新增修复流程。

### 配置和命令

示例配置只开启本项，Judge 使用已有的 `ModelProfile` 选择方式：

```yaml
measures: [interaction]
auto_score: false
concurrency: 1
max_requests: 1
timeout_seconds: 120
max_characters: 200000
```

`max_requests` 在现有框架中是每通样本的预算，不是整个目录只能调用一次。可以在已有 `model` 或 `models.interaction` 字段配置模型、地址和 `api_key_env`，无需增加新的凭据机制。输出长度沿用模型参数，按实际发言数留出足够空间。

规划的命令：

```bash
tau2 metrics run RUN_DIR --config examples/effective-interaction.yaml --only interaction --dry-run
tau2 metrics run RUN_DIR --config examples/effective-interaction.yaml --only interaction
tau2 metrics summarize RUN_DIR
tau2 metrics export RUN_DIR --output interaction-report.json
```

dry-run 沿用当前命令，最多补充目标发言数与输入字符数，不做付费预评分或复杂费用估算。

结果沿用 `artifacts/task_<id>/sim_<id>/diagnostics/evaluations/<profile>/interaction.json`。`tau2 view` 展示概览，逐条原文和理由保存在 JSON 导出中；第一版不新建交互式页面。

## 实现顺序

1. 在 `interaction.py` 写数据模型、计数与聚合，在提示词文件固定边界规则。
2. 实现对话与短事件投影，接入 `TauJsonModel` 的一次调用。
3. 接入 config、suite、CLI，以及 interaction-only 的旧轨迹回退。
4. 添加概览展示、独立 YAML 示例和使用说明。
5. 用几条典型样例确认必要提问、无关提问、混合发言、未知证据和格式失败的行为；在已有 pytest 中用假模型覆盖计数、一次调用和缓存即可。

第一版不设独立验收流程、多人标注项目、反事实实验、复杂审计系统或新的证据采集。先把逐条结果跑出来，发现具体问题后再针对性调整提示词和实现。

# 精简反馈评测

`conciseness` 是可选的、基于 TREC 信息点思想的独立诊断。它按实际请求窗口评价必要信息覆盖、表达长度和业务陈述真实性，不参与任务 reward。当前第一版支持具有完整 diagnostics evidence 和可靠请求阶段的 Gaia2 运行。

## 离线运行

```bash
UV_FROZEN=1 uv run tau2 metrics run RUN_DIR \
  --config examples/concise-feedback.yaml --only conciseness --dry-run
UV_FROZEN=1 uv run tau2 metrics run RUN_DIR \
  --config examples/concise-feedback.yaml --only conciseness
UV_FROZEN=1 uv run tau2 metrics summarize RUN_DIR
UV_FROZEN=1 uv run tau2 metrics export RUN_DIR --output feedback-report.json
```

dry-run、summarize、export 不调用模型。dry-run 给出可用窗口、不可用原因、文本/证据字符数和最多新增模型请求数；这是未计缓存的上界，不是精确 token 或费用估算。

正常每个窗口两次请求：第一阶段使用用户要求和真实执行证据生成信息点，第二阶段使用冻结清单匹配实际回复。已确认没有回复的窗口无需第二次调用。模型默认继承运行的通信裁判，可在 YAML 中分别设置 `models.feedback_nuggets` 和 `models.feedback_alignment`。凭据使用 `api_key_env`，不将密钥写入配置。

默认不增加现有运行的模型调用：原 `measures` 默认值不变，示例使用 `auto_score: false`。首次评分会产生模型费用，但不会重复业务操作。两阶段关闭 provider 自动重试；失败后显式重跑，仅重试未成功阶段。所有请求受现有 `max_requests`、字符上限、超时和并发设置约束。

## 信息点与结果范围

按已送达 Gaia2 stage 划分窗口，窗口内全部 Agent 文字计入长度，不能通过拆成多次发言获得更多基础预算。用户修改、取消和主动要求重述按时间范围解释，后续阶段的请求和执行结果不进入当前窗口。

首版已确认：窗口内存在任意 `complete=false` 的 Agent 发言时，跳过整个窗口，不恢复截断前缀，也不因后续补说而恢复该窗口的精简反馈评分。这是主动选择的样本范围；需单独报告跳过率，不能将其记为零分或从覆盖率中隐藏。纯查询执行评分继续只检查完整交付的发言。

历史采集曾将迟到的 clear 误归给已结束的回复；适配器、缓冲与共享转写已补充 response 级取消证据及时间范围约束。规则本身没有放宽。修复范围、历史影响和仍待验证的部分见 [反馈链路审计](feedback-pipeline-audit.md)。

第一阶段不含当前窗口待测回复，不使用固定成功答案或原 reward。对成功、部分完成、确认失败、结果未知、需要用户输入和未尝试分别确定反馈义务。超时不等于确定失败；缺少操作日志不证明没有执行。

信息点分 required 和 optional，必须引用真实用户要求和适当执行来源。每点是一条可独立判断的命题，金额/单位捆绑；同义命题归为一组，只能获得一次信用。模型负责语义原子化与必要性，程序检查结构、来源 ID、清单完整性和逐字引用，人工验证仍然必要。

第二阶段逐点返回 matched、missing、contradicted 或 uncertain。记录重复、无关内容和实质性错误；先错后明确更正可以满足最终结果覆盖，但不会抹掉已发现的实质错误。无支持证据的错误标签只能触发未知，不能直接清零。

第一版是窗口内批量评价，元数据明确记录 `future_context_visible_within_window: true`。引用时间检查能拒绝明显依靠后来的结果给前面的回复加分，但不能证明 Judge 完全没有未来信息偏差，也不能把环境完成时间当作前台收到结果的时间。

## 分数与缺失值

设 G 为 required 等价组总数、M 为 matched required 组数、O 为 matched optional 组数。L 是实际送达文字 NFC 规范化后的非空白 Unicode 字符数，标点、重复、填充语和礼貌语均计入。

```text
R = M / G
C = min(O, optional_cap * M)
B = base + per_nugget * (M + C)
E = min(1, B / L)
F3 = 10 * E * R / (9 * E + R)
raw_feedback_score = 100 * F3
```

没有匹配任何 required 或确认沉默时 E=0；没有回复时 R=0。G=0 不自动记满分，必须由第一阶段明确判断为不适用或不可用。同义重复不增加 M/O，但仍增加 L。预算每窗口独立，不跨窗口抵扣。

`fidelity_status=material_error` 将候选 `feedback_score` 记为 0，保留 raw 分及错误定位。真实性 uncertain 时不给确定正式分。未知匹配保留分母，给出覆盖率、效率及候选分的上下界。业务失败本身不触发真实性清零。

示例配置现已载入用户基于中文示例选定的初始预算 `B = 10 + 20N`，版本为 `zh-feedback-user-initial-v1`。其中 `N = M + min(O, 0.25M)`；没有可选信息时就是正确覆盖、去重后的必答信息点数。长度按上述非空白字符口径计算。参数文件保留用户选择依据和哈希；`reviewed: true` 表示用户已确认这组预算，不代表已完成 40–60 个窗口的独立标注或留出听评验收。

**没有适用的校准文件时，只输出信息点、覆盖率、真实性和原始长度；长度预算、效率及综合分均为 null。** 当前初始预算只适用于 `zh`，其他语言不沿用中文预算。需要仅收集诊断数据时，可将配置中的 `conciseness.calibration` 设为 `null`。

有可用音频时附带 VAD 语音秒数、已记录回复范围内的播放秒数（含停顿）及每秒必答信息点。音频缺失不抹掉可靠的文本分；这些数据不作为主分，不鼓励通过加速语速提高正式成绩。没有词级对齐时不报告信息点的准确送达时刻。

每个 trial 的主值为所有适用窗口等权平均；任一适用窗口不可评分时，不只对其余窗口生成完整 trial 分数。benchmark 汇总先在同 domain/language/task 内平均可评分 trials，再对任务等权平均，同时给出有效覆盖率、原任务成功率、错误率与各执行结果的分组。micro coverage 仅针对已有清单的窗口，未建立清单的缺失窗口由覆盖率单独展示，不能视为没有义务。

## 人工复核与预算校准

先选取约 40–60 个真实请求窗口，包含成功、部分完成、失败、未知、澄清及不同长度和复杂度；不能仅挑成功短答案。

导出不调用模型：

```bash
UV_FROZEN=1 uv run tau2 metrics feedback-review RUN_DIR --output reviews.json
```

导出的 `blind_input` 供评审员先独立确定信息义务，之后再看 `candidate_replies`。不可用窗口放入 `excluded` 并保留原因。task 哈希分配约 80% development、20% validation，同一任务的全部窗口/trial 必须属于同一 split。

每个 case 的 `reviews` 至少填写两个不同人工评审员的记录，例如：

```json
{
  "reviewer": "human-reviewer-name",
  "approved": true,
  "inventory_correct": true,
  "natural_and_complete": true,
  "required_count": 2,
  "optional_count": 0,
  "concise_response": "订单已取消，但退款尚未成功。"
}
```

两个评审员分别提供自然、完整的精练反馈，确认清单和计数；计数分歧需要先复核。系统不会生成“人工已审核”的样本。若对自动清单有异议，应先修正 rubric 并重新验证，而不是盲目批准。

```bash
UV_FROZEN=1 uv run tau2 metrics feedback-calibrate reviews.json \
  --id zh-feedback-v1 --provenance 'Reviewed development set, 2026-11' \
  --output calibration.json
```

每种语言需要 development 和 task 独立的 validation 样本。只在 development 上拟合非负线性预算，再用 development 长度/预算比值的第 95 百分位作保守缩放。optional_cap 固定为 0.25、F3 的 beta 固定为 3；改变需要新指标/校准版本。输出单独报告留出参考的预算覆盖，留出样本不用于调参。

校准命令提供预算候选，不证明 Judge 已经过人工听评验收，也不自动认定泛化质量合格。先检查未知/错误比例、信息点 precision/recall、重复/漏答/错误值等受控变体，以及难懂的过度压缩，再将认可的候选用于正式报告。

配置文件可引用校准命令的 JSON 输出，相对路径按配置文件所在目录解析，内容会嵌入评分 profile：

```yaml
measures: [conciseness]
auto_score: false
conciseness:
  calibration: ../calibration.json
```

不同语言分别校准。未覆盖的语言只保留覆盖和长度数据，不继承另一语言的预算。长名单、代码和混合语言等特殊类别需要在校准集充分覆盖；第一版不宣称跨这些类别天然公平。

## 审计与兼容

每个评分 profile 的 `conciseness/` 下保存窗口投影、两阶段输入、冻结信息点、原始标签、引用校验和窗口分数。复用现有 `diagnostics/evaluations/<profile>/`、summary 和 `tau2 view`。

跨其他 measures/预算变化复用的成功信息点与请求缓存位于 `diagnostics/conciseness-cache/`。失败解析不进入成功缓存，模型原始输出保留在 `requests/attempts/`；API 失败按类型记录，显式重跑可追踪新 attempt。

不修改历史任务、轨迹、原 reward 或 benchmark pins。历史轨迹没有 diagnostics evidence、已播放文本不可靠、被打断的回复无法验证、阶段边界缺失或时钟不明确时，报告具体不可用原因，不补造答案。无需重放工具来获得新分数。

新增模块的自动化测试覆盖公式、未知区间、证据隔离、重复、缺失、错误引用、冻结清单恢复、人工校准和 CLI。它们验证实现契约，不能替代真实人工复核与听评。

# Gaia2 遗留 Finding 复查与修复计划

## 1. 目标

对第一轮参考答案修复后仍然存在的 unresolved finding 进行第二轮处理。

第二轮在同一个题目级请求中同时完成：

1. 判断每个遗留 finding 是否成立；
2. 判断当前内容是否应该保留；
3. 对确认需要修改的内容生成 JSON Patch；
4. 对证据不足的内容保留原值并记录原因。

本轮是独立的离线数据处理流程，不修改评测系统，不向 Agent、用户模拟器、evaluator、reward 或 runtime 注入 finding 和修复逻辑。

## 2. 输入范围

第二轮只处理当前仍然存在 `unresolved_findings` 的题目，不重新处理已经修复并通过单题检查的内容。

使用以下数据：

```text
data/tau2/domains/gaia2/tasks.json
data/tau2/domains/gaia2/interaction_variants.json
data/archives/gaia2/reference-repair-simple-v1/
data/archives/gaia2/reference-review-v2/responses/reference/
data/tau2/domains/gaia2/snapshots/
```

每道题的遗留 finding 从第一轮修复响应中读取：

```json
{
  "task_id": "...",
  "unresolved_findings": [2, 5, 8]
}
```

再根据编号回到对应原始审查结果：

```text
data/archives/gaia2/reference-review-v2/responses/reference/<task_id>.json
```

不能直接把整个 `unresolved.jsonl` 交给模型，因为其中可能混有重复 finding、模型请求失败、非法 patch 和变体回退记录。

## 3. 生成第二轮任务清单

新增离线命令：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_unresolved.py unresolved-prepare \
  --input-root data/archives/gaia2/reference-repair-simple-v1 \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --data-root data/tau2/domains/gaia2 \
  --out data/archives/gaia2/reference-repair-simple-v2
```

每道题生成一条清单记录：

```json
{
  "task_id": "...",
  "kind": "base",
  "source_task_id": "...",
  "finding_indexes": [2, 5, 8],
  "findings": [
    {
      "index": 2,
      "status": "needs_evidence",
      "severity": "high",
      "title": "...",
      "evidence": ["..."],
      "impact": "...",
      "recommendation": "...",
      "related_event_ids": ["..."]
    }
  ],
  "current_repair_status": "unresolved"
}
```

不预设问题类型，也不为不同 finding 类型建立不同处理器。同一道题的全部遗留 finding 放在同一个输入包中处理。

## 4. 精简复查输入包

每道题生成：

```text
data/archives/gaia2/reference-repair-simple-v2/inputs/<task_id>.json
```

输入包的目标是让模型拥有判断和修复所需的证据，同时避免传入完整任务中与当前 finding 无关的内容。

输入包结构：

```json
{
  "task_id": "...",
  "kind": "base",
  "source_task_id": "...",
  "user_request": "...",
  "current_reference_answer": "...",
  "relevant_events": [],
  "relevant_stages": [],
  "relevant_checks": [],
  "relevant_criteria": {},
  "unresolved_findings": [],
  "evidence": []
}
```

### 4.1 始终保留

- 用户实际请求；
- 当前第一轮修复后的参考答案；
- finding 指向的事件；
- finding 指向的 stage；
- finding 需要修改的当前字段；
- 相关证据；
- 涉及时间时的 `start_time` 和 `timezone`。

### 4.2 默认省略

- 完整 `scenario.events`；
- 完整 `scenario.stages`；
- 无关 stage；
- 无关 event；
- 无关 checks；
- 无关 criteria；
- `source_id`、source hash、runtime hash；
- 完整 snapshot；
- 其他题目的 review；
- 已解决的 finding。

## 5. 相关事件提取

根据以下信息机械选择事件：

- finding 的 `related_event_ids`；
- evidence 中出现的 event ID；
- recommendation 中出现的 event ID；
- JSON path 中的事件索引；
- finding 文本中明确出现的事件名；
- patch 可能修改的事件。

相关事件默认只传：

```json
{
  "id": "...",
  "kind": "reference",
  "tool": "...",
  "arguments": {},
  "dependencies": []
}
```

以下字段默认省略：

- `delay_seconds`；
- `time_comparator`；
- `channel`；
- `match_fields`；
- `semantic_fields`；
- 无关事件；
- 无关事件的完整参数。

只有 finding 明确涉及时间、评分匹配、语义匹配或 Agent 回复时，才加入对应字段。

如果只判断依赖关系，相关事件只需保留 `id`、`kind` 和 `dependencies`。如果判断收件人、标题、日期或内容，则保留相关事件的完整 `arguments`。

## 6. 相关阶段和评分字段提取

根据 finding 中的 stage 编号、事件归属、JSON path 和 recommendation 选择 stage。

默认保留：

```json
{
  "stage": 1,
  "request": "...",
  "event_ids": ["..."]
}
```

按 finding 再加入：

- 阶段顺序问题：`relation`、`preconditions`；
- 检查问题：相关 `checks`；
- 回复问题：`communicate_info`；
- NL 评分问题：相关 `nl_assertions`；
- 环境状态问题：`env_assertions`；
- reward 配置问题：`reward_basis`。

不传无关 stage、无关 checks、无关 criteria、通用 NL assertion 和空的 env_assertions。

尤其不能默认发送一个 stage 的全部 checks。复杂题的 checks 可能占用十万字符以上。

## 7. 快照证据提取

默认不把完整 snapshot 放进模型输入。脚本根据 finding 的路径和关键词提取相关证据：

- 联系人记录；
- 邮箱、地址、日期、金额；
- 房源、订单和日历事件；
- 相关应用的候选集合；
- 用于排序、计数、最小值和最大值的完整数据集合。

示例：

```text
收件人问题       → 用户邮箱、目标联系人、相关邮件事件
依赖问题         → 相关事件和依赖图
最低值问题       → 完整 crime_data
保存数量问题     → 相关 saved_apartments
空闲时间问题     → 目标日期的日历事件
```

需要重新计算排序、计数或最小值时，必须保留完整的相关候选集合。证据不足时返回 `unresolved`，不能让模型猜测。

## 8. 一次模型请求同时完成判断和修复

每道题只调用一次模型。模型同时处理该题的全部遗留 finding，并返回判断和 patch。

允许的 decision：

```text
fix
keep
unresolved
```

返回格式：

```json
{
  "task_id": "...",
  "decisions": [
    {
      "finding_index": 2,
      "decision": "fix",
      "reason": "...",
      "evidence": ["..."]
    },
    {
      "finding_index": 5,
      "decision": "keep",
      "reason": "...",
      "evidence": ["..."]
    },
    {
      "finding_index": 8,
      "decision": "unresolved",
      "reason": "缺少完整快照证据"
    }
  ],
  "operations": [
    {
      "op": "replace",
      "path": "/scenario/events/12/arguments/recipients",
      "value": ["..."]
    }
  ],
  "summary": "..."
}
```

规则：

- `fix` 必须有对应 patch；
- `keep` 不生成 patch；
- `unresolved` 保留当前内容；
- 不凭空生成联系人、地址、日期、金额、ID、数量或状态；
- 不默认修改用户原始请求；
- 不用当前参考答案自身作为新事实来源。

## 9. Patch 应用机制

模型返回的 patch 先在当前 v1 数据的内存副本上应用：

```text
当前 v1 题目
    → deepcopy
    → 应用全部 operations
    → 单题轻量检查
    → 成功后写入 v2
```

默认禁止修改：

```text
/id
/user_scenario
/scenario/source_id
/scenario/source_sha256
/scenario/snapshot
```

用户请求保持不变。模型返回的所有 JSON Pointer 必须经过本地验证。

如果同一题有一个 patch 失败，整批 patch 不提交，当前题目保持 v1 内容，并记录为 `unresolved` 或 `failed`。

## 10. Base 与变体

按数据来源关系处理：

```text
base
→ repetition / mind_change
→ AddTask / Modify / Cancel
```

规则：

- base 先从 v1 生成 v2；
- 变体重新绑定 v2 的 base；
- 变体只应用自己的遗留 patch；
- 变体不复制完整 source task；
- source hash 根据 v2 base 重新计算；
- 变体无法 materialize 时只回退该变体；
- 变体失败不影响 base。

## 11. 轻量单题检查

每道题 patch 应用后立即检查：

1. JSON 可以加载；
2. `Task` 或 `InteractionVariant` 可以通过 Pydantic；
3. event ID 没有重复；
4. dependency 目标存在；
5. 只有修改依赖时才检查循环；
6. 变体可以从 source task materialize；
7. 修改 reference event 或 stage check 时，复用该题已有的 reference replay。

不新增 evaluator、reward、runtime 逻辑，不把第二轮流程接入正式评测。

不在全部题目处理完成后再做一次重型全量验收。每道题的结果随题目立即保存。

## 12. 输出目录

```text
data/archives/gaia2/reference-repair-simple-v2/
├── unresolved-input.jsonl
├── inputs/
├── requests/
├── responses/
├── repaired-tasks.json
├── repaired-interaction-variants.json
├── repaired-all-tasks.json
├── changes.jsonl
├── unresolved.jsonl
└── summary.json
```

`changes.jsonl` 示例：

```json
{
  "task_id": "...",
  "status": "fixed",
  "decisions": {
    "2": "fix",
    "5": "keep",
    "8": "unresolved"
  },
  "changed_paths": [
    "/scenario/events/12/arguments/recipients"
  ],
  "before_sha256": "...",
  "after_sha256": "...",
  "validation": {
    "task_model": true,
    "dependencies": true,
    "variant_materialization": true
  }
}
```

`unresolved.jsonl` 示例：

```json
{
  "task_id": "...",
  "finding_index": 8,
  "reason": "完整快照中没有对应联系人记录",
  "current_content_kept": true
}
```

## 13. 输入量控制

第二轮不发送完整 Task。目标输入规模：

```text
普通题：5k–20k 字符
复杂题：20k–50k 字符
硬上限：120k 字符
```

超过软上限时按以下顺序压缩：

1. 删除无关事件；
2. 删除无关 stage；
3. 删除无关 checks 和 criteria；
4. 合并重复 evidence；
5. 只保留相关快照记录；
6. 如果关键证据仍然超过限制，返回 `unresolved`，不截断关键证据后猜测。

## 14. 运行命令

准备遗留输入：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_references.py unresolved-prepare \
  --input-root data/archives/gaia2/reference-repair-simple-v1 \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --data-root data/tau2/domains/gaia2 \
  --out data/archives/gaia2/reference-repair-simple-v2
```

执行第二轮复查和修复：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_unresolved.py unresolved-repair \
  --env-file /hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env \
  --config docs/gaia2-reference-review.deepseek.yaml \
  --out data/archives/gaia2/reference-repair-simple-v2
```

支持断点续跑：

- 已完成的模型响应不重复请求；
- 请求失败的题目可单独重试；
- 已应用的 patch 不重复应用；
- 请求缓存按题目 hash、输入包 hash 和修复协议版本区分。

## 15. 最终结果

第二轮只需要汇总每道题的单题结果：

```text
fixed
kept
unresolved
failed
```

核心机制是：本地保留完整数据，模型只看当前 finding 相关字段；模型一次完成判断和 patch；本地立即应用和轻量检查；整个流程独立于正式评测系统。


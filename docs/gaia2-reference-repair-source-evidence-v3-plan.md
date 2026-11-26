# Gaia2 遗留题来源复核与修复计划

## 1. 目标

对第二轮仍未解决的题目重新回到原始来源证据，确认 finding 是否成立后再修改参考数据。

当前要处理的对象包括：

- 442 道仍有 unresolved finding 的题；
- 170 道本地 patch 应用失败的题。

本轮重点是恢复和验证来源证据：

- 原始任务；
- source task；
- 内容哈希 snapshot；
- stage evidence；
- reference events；
- 必要的 reference replay；
- 变体 provenance 和 source hash。

本轮使用独立离线机制，不修改 Agent、用户模拟器、evaluator、reward、runtime 或正式评测流程。

## 2. 当前数据来源

### 2.1 初始 snapshot

每道题通过：

```text
task.scenario.snapshot
→ data/tau2/domains/gaia2/snapshots/<snapshot_id>.json.gz
```

读取初始应用状态。

snapshot 中包含：

- Contacts；
- InternalContacts；
- Messages；
- Chats；
- Emails；
- Calendar；
- RentAFlat；
- City；
- Cabs；
- Shopping；
- Files。

snapshot 是事实来源。模型不能根据第一轮 finding 中的文字替代 snapshot 中的实际值。

### 2.2 Stage evidence

每个 stage 可能包含：

```text
stage.evidence
stage.checks
stage.criteria
```

evidence 的来源包括：

```text
snapshot
state
result
```

每条 evidence 通过 JSON pointer 指向具体值，例如：

```text
/Contacts/contacts/<id>/email
/RentAFlat/apartments/<id>/zip_code
/Messages/conversations/<id>/messages/...
```

### 2.3 Reference events

如果 evidence 来源是 `state` 或 `result`，需要执行相关 reference events 或最小事件子图，再从 checkpoint 或工具结果中解析 evidence。

### 2.4 Variants

变体通过以下关系绑定来源题：

```text
interaction_variants.json
provenance.json
source_task_id
source_sha256
```

先确认 source task 的事实，再确认变体自己的请求和 reference 内容。

## 3. 处理范围和批次

不一次性处理 442 道题。

每个批次建议包含 10 道题。每个批次独立保存状态：

```text
batch-0001
batch-0002
batch-0003
...
```

每道题仍然使用一个独立模型请求。批次只是任务管理单位，不把多道题拼成一个 prompt。

批次流程：

```text
读取一批题
→ 逐题构造来源证据
→ 逐题判断和生成 patch
→ 逐题应用和检查
→ 保存批次结果
→ 继续下一批
```

中断后从未完成题继续，成功题不重复请求。

## 4. 第一步：建立来源证据包

新增离线工具，例如：

```text
scripts/build_gaia2_source_evidence.py
```

对每道遗留题读取：

- 当前 v2 任务；
- 原始 source task；
- unresolved findings；
- finding 中的 event ID；
- finding 中的 JSON path；
- finding 中的 stage；
- task snapshot；
- stage evidence；
- reference events；
- variant provenance。

每条 finding 生成一个来源证据记录：

```json
{
  "task_id": "...",
  "finding_index": 2,
  "claim": "301 00 是最低 violent crime ZIP",
  "source": "snapshot",
  "snapshot_id": "...",
  "app": "City",
  "pointer": "/crime_data",
  "value": {},
  "derived_value": "301 00",
  "derivation": "遍历完整 crime_data 后取最小值",
  "status": "verified"
}
```

来源状态只有：

```text
verified
contradicted
missing
```

### 4.1 直接读取 snapshot

以下事实直接从 snapshot 读取：

- 联系人字段；
- 邮箱和地址；
- 房源属性；
- 保存状态；
- 订单和商品；
- 日历事件；
- crime data；
- 消息和邮件历史。

### 4.2 本地计算派生事实

以下结果由 Python 根据完整数据计算：

- 最小值和最大值；
- 排序；
- 数量；
- 候选筛选；
- 房源或商品排名；
- 时间窗口；
- 日历冲突；
- 统计差值。

模型只看计算结果和来源路径，不负责自行心算。

### 4.3 Replay state/result evidence

如果 finding 需要 stage 状态或工具结果：

```text
读取 reference events
→ 执行最小依赖事件子图
→ 建立 stage checkpoint
→ 读取 result/state pointer
→ 保存实际值
```

不需要为每道题重放完整任务，只重放当前 finding 所需的最小事件集合。

## 5. 第二步：构造精简输入

每道题的模型输入包括：

```json
{
  "task_id": "...",
  "kind": "base",
  "user_request": "...",
  "current_target": "相关当前内容",
  "unresolved_findings": [],
  "verified_evidence": [],
  "relevant_events": [],
  "relevant_stages": [],
  "source_context": {}
}
```

保留：

- 用户请求；
- 当前 reference answer；
- finding 指向的事件；
- finding 指向的 stage；
- 已验证的来源值；
- 必要的计算过程；
- 变体 source task 的相关部分。

省略：

- 完整 snapshot；
- 无关应用状态；
- 无关 events；
- 无关 stages；
- 无关 checks；
- 其他题目的 findings；
- source hash 和 runtime metadata；
- 已经解决的 finding。

如果需要重新计算完整排序、数量或最小值，传入完整的相关候选集合。

## 6. 第三步：逐题判断并修复

每道题一次模型请求，同时完成判断和 patch。

模型输出：

```json
{
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
      "reason": "来源仍不足"
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

- 来源明确证明错误：`fix`；
- 来源证明当前内容正确：`keep`；
- 来源不足：`unresolved`；
- 不猜联系人、地址、日期、金额、ID、数量或状态；
- 不修改用户原始请求，除非来源能证明请求文本有错误；
- patch 必须对应某个 `fix` decision。

不预先建立问题类型工作流。每道题按自己的 finding 和来源证据处理。

## 7. 第四步：应用 patch

patch 在当前 v2 任务副本上原子应用：

```text
当前 v2 题目
→ deepcopy
→ 应用全部 operations
→ 当前题轻量检查
→ 成功后写入 v3
```

一条操作失败时，整道题的 patch 不提交。

默认保护：

```text
/id
/user_scenario
/scenario/source_id
/scenario/source_sha256
/scenario/snapshot
```

对于变体：

- source task 的问题修改到 source task；
- 变体自身的问题修改到 interaction variant；
- source hash 根据修改后的 source task 重新计算；
- 变体 materialize 失败时回退该变体；
- 变体失败不回退 source task。

## 8. 170 道 patch 失败题

170 道 failed 题先不重新审查事实。

处理顺序：

1. 读取已有模型响应；
2. 归一化 JSON Pointer；
3. 修正缺少 `/scenario` 前缀；
4. 处理 `add`、`replace` 的字段存在性；
5. 修正变体字段类型；
6. 重新计算 source hash；
7. 在当前 v2 副本上重新应用；
8. 通过单题检查后写入 v3。

只有没有可用响应或 patch 无法恢复的题，才重新请求模型。

## 9. 单题轻量检查

每道题 patch 应用后立即检查：

1. JSON 可以加载；
2. `Task` 或 `InteractionVariant` 可以通过 Pydantic；
3. event ID 不重复；
4. dependency 目标存在；
5. 修改依赖时检查是否成环；
6. 变体可以从 source task materialize；
7. 修改 reference event、evidence 或 check 时复用该题已有的 reference replay。

不做全量重型验收，不新增正式评测系统逻辑。

## 10. 输出目录

使用新的独立版本：

```text
data/archives/gaia2/reference-repair-simple-v3/
├── source-evidence/
├── inputs/
├── requests/
├── responses/
├── batches/
├── repaired-tasks.json
├── repaired-interaction-variants.json
├── repaired-all-tasks.json
├── changes.jsonl
├── unresolved.jsonl
└── progress.json
```

每道题的变更记录：

```json
{
  "task_id": "...",
  "batch_id": "batch-0001",
  "status": "fixed",
  "evidence_status": "verified",
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
    "reference_replay": true
  }
}
```

## 11. 执行命令建议

建立来源证据：

```bash
UV_FROZEN=1 .venv/bin/python scripts/build_gaia2_source_evidence.py \
  --input-root data/archives/gaia2/reference-repair-simple-v2 \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --data-root data/tau2/domains/gaia2 \
  --out data/archives/gaia2/reference-repair-simple-v3 \
  --batch-size 10
```

逐题复查和修复：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_source_evidence.py \
  --input-root data/archives/gaia2/reference-repair-simple-v2 \
  --evidence-root data/archives/gaia2/reference-repair-simple-v3 \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --data-root data/tau2/domains/gaia2 \
  --out data/archives/gaia2/reference-repair-simple-v3 \
  --batch-size 10 \
  --concurrency 1
```

每批完成后保存 checkpoint。批次之间不共享未提交的修改。

## 12. 完成标准

每道题有自己的结果：

```text
fixed
kept
unresolved
failed
```

最终不再跑一次 1200 道题的重型全量验收。每道题的来源记录、patch 和单题检查结果已经保存在 v3 中。

核心流程是：

```text
原始 snapshot / stage evidence
→ 本地解析和计算
→ 逐题确认来源
→ 逐题生成 patch
→ 当前题立即检查
→ 保存小批次结果
```

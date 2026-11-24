# Gaia2 1200 道题参考答案简化修复计划

## 1. 目标

根据已经完成的 DeepSeek 检查结果，直接修复 Gaia2 的 1200 道任务，使参考答案尽可能符合：

- 用户的实际要求；
- 任务的初始数据和状态；
- 正确的工具操作、参数和执行顺序；
- 阶段回复和最终回复中的真实结果；
- 当前评测使用的评分条件。

本计划只解决参考答案和相关评分内容的修复，不重新设计整套审查系统，也不要求人工逐题重新审计 1200 道题。

## 2. 输入和输出

### 输入

- 当前 Gaia2 任务数据；
- 已保存的 DeepSeek 检查结果；
- 现有任务快照、参考事件、参考回复和评分条件；
- 现有任务和变体的来源关系。

当前任务由 800 道 base、100 道 repetition、100 道 mind_change、100 道 AddTask、50 道 Modify 和 50 道 Cancel 组成，共 1200 道。

### 输出

生成一份新的修复版数据，至少包括：

- 修复后的 1200 道任务；
- 修复前后的差异记录；
- 每道题的修复状态；
- 无法确定、暂时未修改的题目列表；
- 基础验证报告。

原始任务、原始检查结果和原始快照全部保留，不直接覆盖。

## 3. 直接批量修复流程

### 第一步：准备修复输入

对每道题读取并合并：

1. 原始任务内容；
2. 当前参考答案和参考事件；
3. DeepSeek 对该题的检查结果；
4. 任务初始快照或相关证据；
5. 任务所属类型及来源题信息。

不再先建立复杂的人工分类、根因系统或完整审计台账。已有检查结果直接作为修复依据。

### 第二步：生成修正版任务

对每道题执行一次批量修复。修复模型需要：

- 读取 DeepSeek 指出的错误、遗漏、依赖问题、参数问题和评分问题；
- 根据原始任务和快照判断修改内容；
- 只修改检查结果支持的部分；
- 保留没有明确问题的内容；
- 输出完整的修正版任务和修改说明。

修复范围可以包括：

- 参考操作；
- 工具参数；
- 事件 ID 和依赖关系；
- 阶段归属；
- 查询、筛选、排序和计数结果；
- 阶段回复和最终回复；
- `checks`、`criteria` 和 `env_assertions`；
- AddTask、Modify、Cancel 的变体专属参考内容。

参考答案中明确错误的内容直接修正，明确遗漏的内容直接补齐，明确多余且没有用户依据的内容删除。

### 第三步：处理证据不足的检查结果

DeepSeek 的检查结果中如果标记为 `needs_evidence`，先让修复模型结合原始任务和完整快照再次判断：

- 如果能够确认错误，直接修复；
- 如果能够确认当前参考答案正确，保持原内容；
- 如果仍然无法判断，保持原内容并记录到待处理列表。

不根据没有证据的猜测修改联系人、地址、日期、金额、对象 ID 或最终数量。

### 第四步：生成修复版文件

修复结果写入新的版本目录，例如：

```text
data/archives/gaia2/reference-repair-simple-v1/
├── repaired-tasks.json
├── repaired-interaction-variants.json
├── changes.jsonl
├── unresolved.jsonl
├── manifest.json
└── validation-report.json
```

`changes.jsonl` 每行对应一道题，至少记录：

```json
{
  "task_id": "...",
  "status": "changed",
  "changed_paths": [
    "/scenario/stages/1/events/2/dependencies",
    "/evaluation_criteria/communicate_info"
  ],
  "reason": "修正收件人并补充必要依赖",
  "source_findings": ["..."],
  "before_sha256": "...",
  "after_sha256": "..."
}
```

`unresolved.jsonl` 记录仍然无法确认的题目和对应 finding，不阻塞其他题目的修复。

## 4. 变体处理

变体按来源关系处理：

- base 的修复传播到对应的 repetition 和 mind_change；
- AddTask、Modify、Cancel 保留自己的新增或修改逻辑；
- 变体不能因为批量处理而丢失来源题关系；
- 修复后重新生成变体并检查 source ID 和 hash；
- 不直接覆盖原始变体文件。

不要求为每个变体重新设计一套流程，只需要确保 base 修复能够正确传播，变体独有内容仍然有效。

## 5. 基础验证

批量修复完成后只做必要验证：

### 数据结构

- JSON 可以加载；
- 任务总数仍为 1200；
- 任务 ID 唯一；
- 参考事件 ID 唯一；
- 所有依赖目标存在；
- 没有明显循环依赖。

### 来源关系

- 400 道变体仍能找到正确的 source task；
- source ID 和 source hash 正确；
- base 修改能够反映到相关变体。

### 参考内容

- 参考答案中的联系人、地址、日期、金额和对象 ID 能在任务或快照中找到依据；
- 阶段回复不声称尚未完成的动作；
- 参考操作不包含用户没有要求且没有证据支持的额外动作；
- 用户明确要求的操作和结果没有被遗漏。

### 评测验证

- 运行现有 Gaia2 数据验证命令；
- 对修改过的题目执行参考 replay 或等价的离线检查；
- 检查修改后的评分条件能够接受正确参考答案；
- 检查明显错误的结果不会因为修复而通过。

## 6. 修复状态

每道题只需要记录一个简单状态：

```text
changed       已根据检查结果修改
unchanged     检查结果不足以支持修改，保留原题
validated     已修改并通过基础验证
unresolved    仍有无法确认的问题
failed        修复或验证失败，等待重新处理
```

不要求为每条 finding 建立复杂的独立审批流程。每道题保留修改前后 hash、修改路径、修改原因和来源 finding 即可追溯。

## 7. 执行顺序

实际执行按以下顺序进行：

1. 读取并冻结当前任务和检查结果；
2. 批量生成 1200 道题的修复输入；
3. 批量生成修正版任务；
4. 生成修改差异和未解决列表；
5. 重新生成相关变体；
6. 运行基础结构、来源和参考内容验证；
7. 对验证失败的题目进行一次定向修复；
8. 输出最终修复版数据和报告。

## 8. 完成标准

本次工作完成时应满足：

1. 1200 道题都有修复处理结果；
2. 明确错误的参考内容已经修改；
3. 明确遗漏的操作或评分条件已经补齐；
4. 没有证据的问题被单独列出，没有被强行猜测修改；
5. 原始数据和原始检查结果未被覆盖；
6. 修复版任务可以加载和运行；
7. 变体来源关系保持正确；
8. 基础 Gaia2 验证通过；
9. 每道修改过的题目都有修改路径、原因和前后 hash。

这个方案的重点是直接利用已经完成的 DeepSeek 检查结果完成批量修复，把人工工作集中到自动修复失败或证据确实不足的少数题目上。

## 9. 当前实现

批量修复脚本为 `scripts/repair_gaia2_references.py`。它使用 JSON Pointer 操作记录最小修改，逐题缓存模型结果，支持中断后继续运行，并将结果写入独立目录。

先生成 1200 道题的修复输入：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_references.py prepare \
  --out data/archives/gaia2/reference-repair-simple-v1
```

无模型调用的全量检查：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_references.py repair \
  --out data/archives/gaia2/reference-repair-simple-v1 --dry-run
```

已有审查结果中能够直接确认的少量依赖和收件人修复，可以先生成候选计划；修复器会自动应用这些候选，其余题目继续交给模型判断：

```bash
UV_FROZEN=1 .venv/bin/python scripts/plan_gaia2_reference_repairs.py \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --data-root data/tau2/domains/gaia2 \
  --out data/archives/gaia2/reference-repair-simple-v1/rule-based-plan.jsonl
```

配置好 `DEEPSEEK_API_KEY` 后执行真实修复。可以直接指定现有环境文件，结果可以断点续跑：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_references.py repair \
  --config docs/gaia2-reference-review.deepseek.yaml \
  --env-file /hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env \
  --out data/archives/gaia2/reference-repair-simple-v1
```

验证修复版：

```bash
UV_FROZEN=1 .venv/bin/python scripts/repair_gaia2_references.py validate \
  --out data/archives/gaia2/reference-repair-simple-v1
```

脚本会生成 `repaired-tasks.json`、`repaired-interaction-variants.json`、`repaired-all-tasks.json`、`changes.jsonl`、`unresolved.jsonl`、`manifest.json` 和 `validation-report.json`。当前环境如果没有有效的 `DEEPSEEK_API_KEY`，只能完成输入准备和 dry-run，不能生成模型驱动的实际修改。

当前已使用 `/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env` 完成一轮全量修复：1200 道题中 1131 道有修改，2 道保持不变，67 道因证据不足或模型请求失败保留原内容；1000 道任务、200 个变体和 1200 道运行视图的基础验证已通过。

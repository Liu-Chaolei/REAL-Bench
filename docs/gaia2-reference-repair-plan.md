# Gaia2 参考答案与用户协议修复实施计划

状态：第 0 批工具链部分已实施，reference 数据和评分修复尚未开始。当前已经完成 1200 道题的离线审查材料、来源正确的修复队列和队列校验器。

本计划包含两条需要分开验收的工作线：

1. 修复 Gaia2 用户控制信号和中立用户提示的作用域；
2. 把 reference 审查结果转化为可追溯、可回滚的数据和评分修复。

两条工作线都完成后，才发布新的 Gaia2 reference repair 版本。

## 1. 目标与边界

### 1.1 目标

- 让 Gaia2 用户使用中立的任务委托者角色，不再在 Gaia2 提示中生成 `###TRANSFER###`；
- 保持非 Gaia2 域原有 transfer 行为和兼容性；
- 确认 reference finding 是否成立，再修改 reference 操作、参数、依赖、回复、checks 或公共评分规则；
- 保持 base、repetition、mind change、AddTask、Modify、Cancel 的来源关系和 hash 一致；
- 保存每次修改前后的值、证据、patch、验证结果和受影响变体；
- 让修复后的数据可以重新生成、验证、回放和回滚。

### 1.2 边界

- 不把模型 finding 数量直接当作错误数量；
- `needs_evidence` 不自动升级为错误；
- 不在正式评测运行时调用审查模型；
- 不覆盖原始 Gaia2 数据和原始审查归档；
- 不在本批次偷偷改变整体 reward 定义；
- 不要求完整 Agent 重跑 1200 道题，但对修改过的结构和公共规则做代表性 reference replay；
- 不把 Gaia2 的 no-transfer 决策扩散到 retail、airline、telecom、intake 或 Gym 的通用用户协议。

## 2. 现状与必须先解决的问题

当前输入位于：

```text
data/archives/gaia2/reference-review-v2/
```

主要产物：

- `inventory.json`：1200 道题的类型和来源关系；
- `inputs/`：每题完整结构化审查材料；
- `responses/understanding/`：800 道 base 的需求理解结果；
- `responses/reference/`：1200 道题的 reference 审查结果；
- `reference-summary.json`：按任务汇总的 finding。

当前初版队列位于：

```text
data/archives/gaia2/reference-repair-v1/repair-queue.jsonl
```

开始数据修复前必须解决以下问题：

1. 队列生成器当前把变体的 `source_task_id` 写成变体自身 ID，必须改为从 `inventory.json` 或 `provenance.json` 读取；
2. 队列优先级当前只看 confirmed finding，可能把包含 high/critical `needs_evidence` 的任务排到 low；
3. 当前 no-transfer 修改了共享用户协议，需要收回为 Gaia2 局部行为；
4. Gaia2 v1/v2 版本、共享 prompt fixture、实施记录和代码版本不一致，需要统一；
5. 需要增加 queue validator、repair ledger、patch 应用器和修复后 artifact validator。

审查结果的统计必须由脚本重新生成，不能手写在文档中。文档中的统计只作为最近一次生成结果，不作为数据源。

## 3. 工作线 A：修复用户控制信号作用域

### 3.1 规则决定

采用以下协议：

- 通用 tau2 用户协议继续支持 `###STOP###`、`###TRANSFER###` 和 `###OUT-OF-SCOPE###`；
- Gaia2 中立用户指南只描述 `###STOP###` 和 `###OUT-OF-SCOPE###`；
- Gaia2 的阶段 controller 只消费前两个控制信号，不把 transfer 作为阶段推进信号；
- `TRANSFER` 常量保留，避免破坏外部 import 和非 Gaia2 域行为；
- transfer tool、Agent 侧 transfer policy 和 judge 中的 transfer 清理逻辑继续保留。

### 3.2 实现范围

保留或恢复通用行为的文件：

- `src/tau2/user/user_simulator_base.py`
- `src/tau2/user/user_simulator.py`
- `src/tau2/user/user_simulator_streaming.py`
- `src/tau2/gym/gym_agent.py`
- `src/tau2/user/guidelines.py`

Gaia2 专属行为保留在：

- `src/tau2/user/gaia2_guidelines.py`
- `src/tau2/orchestrator/scenario_stages.py`
- Gaia2 builder 的 domain-specific override 入口。

### 3.3 版本和文档

如果 no-transfer 作为 Gaia2 行为发布，版本统一使用：

```text
gaia2-neutral-user-v2-no-transfer
```

同步更新：

- `src/tau2/user/gaia2_guidelines.py`；
- `docs/gaia2.md`；
- `docs/gaia2-neutral-user-plan.md`；
- `docs/gaia2-neutral-user-results.md`；
- 离线 prompt 示例；
- Gaia2 builder 测试和版本断言。

实施记录不得再声称“没有修改用户模拟器基类”，除非共享文件的差异已经恢复。

### 3.4 验收测试

至少覆盖：

- Gaia2 text prompt 不包含 transfer 指南；
- Gaia2 voice prompt 和 role reminder 不包含 transfer 指南；
- retail text/voice 仍包含并消费 transfer；
- Gym 用户遇到 `###TRANSFER###` 仍然停止；
- `STOP`、`TRANSFER`、`OUT-OF-SCOPE` 混合内容保持原有优先级；
- persona、voice settings、语言和声学配置不被修改。

## 4. 工作线 B：修复队列和 provenance

### 4.1 队列数据来源

`scripts/triage_gaia2_repairs.py` 必须读取审查目录的 `inventory.json`：

```python
inventory_by_id = {
    row["id"]: row
    for row in inventory["rows"]
}
```

每条 finding 使用 inventory 中的元数据：

```python
row = inventory_by_id[finding["task_id"]]
kind = row["kind"]
source_task_id = row["source_task_id"]
```

不得从 finding 本身推导来源 ID。base、repetition、mind change、AddTask、Modify、Cancel 的来源关系必须以 inventory 为准。

### 4.2 Queue row 结构

每道题只生成一个 queue row：

```json
{
  "task_id": "...",
  "kind": "addtask",
  "source_task_id": "...base-id...",
  "source_sha256": "...",
  "priority": "high",
  "issue_priority": "medium",
  "evidence_priority": "high",
  "confirmed_issue_count": 2,
  "needs_evidence_count": 5,
  "areas": {
    "reference_id_or_index": 2,
    "scoring_coverage_or_overconstraint": 3
  },
  "confirmed": [],
  "needs_evidence": [],
  "repair_status": "pending"
}
```

每条 finding 应保留原始审查记录路径、review round、input key、原始和规范化 status/severity、原始 area、相关 event IDs、evidence、impact 与 recommendation。

### 4.3 Status 和 severity 规范化

允许的最终状态：

```text
confirmed_issue
false_positive
needs_evidence
fixed
blocked
shared_rule_fix
by_design
```

允许的严重级别：

```text
critical
high
medium
low
info
```

`moderate`、`medium-high`、`warning` 等模型输出必须显式转换并保存原值。未知值不能静默降为 low。

### 4.4 Priority 规则

同时计算：

- `issue_priority`：只考虑 `confirmed_issue`；
- `evidence_priority`：考虑 `confirmed_issue` 和 `needs_evidence`；
- `priority`：第一批排队使用 `evidence_priority`，但保留 issue priority 供报告使用。

严重级别顺序为：

```text
critical > high > medium > low > info
```

## 5. Queue validator

新增：

```text
scripts/validate_gaia2_repair_queue.py
```

必须检查：

1. queue 中有且只有 1200 个任务；
2. queue ID 与 inventory ID 完全相同；
3. 每个非 base 任务的 source ID 与 inventory 一致；
4. source task 存在于 `tasks.json`；
5. source hash 与当前 base task 一致；
6. kind、variant type 和 source ID 一致；
7. 每道题至少有一个 confirmed 或 needs-evidence finding，除非明确记录为 no_issue；
8. 所有 severity、status、area 都是规范值；
9. 统计值由 queue 实际内容计算，不能依赖文档常量。

命令：

```bash
UV_FROZEN=1 .venv/bin/python scripts/triage_gaia2_repairs.py \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --out data/archives/gaia2/reference-repair-v1/repair-queue.jsonl

UV_FROZEN=1 .venv/bin/python scripts/validate_gaia2_repair_queue.py \
  --review-dir data/archives/gaia2/reference-review-v2 \
  --queue data/archives/gaia2/reference-repair-v1/repair-queue.jsonl
```

## 6. Repair ledger 和证据目录

目录结构：

```text
data/archives/gaia2/reference-repair-v1/
├── repair-queue.jsonl
├── repair-ledger.jsonl
├── patches/
├── evidence/
├── rejected/
├── pending/
├── validation/
└── repair-summary.md
```

每条 ledger 至少包含：

```json
{
  "repair_id": "rep-000001",
  "task_id": "...",
  "source_task_id": "...",
  "kind": "base",
  "stage": 1,
  "area": "dependencies",
  "severity": "high",
  "status": "confirmed_issue",
  "review_record": "responses/reference/task.json",
  "finding_title": "...",
  "old_value": "...",
  "new_value": null,
  "reason": "...",
  "evidence": ["evidence/rep-000001.json"],
  "affected_variants": [],
  "patch": null,
  "validation": [],
  "reviewer": "...",
  "updated_at": "..."
}
```

状态变化必须可追踪：

```text
pending → needs_evidence → confirmed_issue → fixed
pending → false_positive
pending → by_design
confirmed_issue → blocked
confirmed_issue → shared_rule_fix
```

`fixed` 记录必须同时有 old/new value、evidence、patch 和 validation。

## 7. 根因聚类和人工确认

审查结果不能直接按 16875 条 finding 逐条改数据。先进行公共根因聚类：

- 固定随机 ID；
- 错误 event 引用；
- 依赖缺失或依赖循环；
- stage 和 event 错配；
- exact text 过度约束；
- criteria 缺失或读取错误阶段；
- ENV assertion 不完整；
- 查询答案事实错误；
- AddTask 新目标缺失；
- Modify/Cancel 绑定和 native scoring 错误。

每个根因先抽取代表题，读取完整 snapshot、事件图和评分入口，确定它属于单题数据、一类任务的 authoring 规则、公共评分代码、变体展开逻辑、运行时阶段逻辑或文档/provenance。

程序检查只能产生候选，不能依据结构异常直接修改用户要求、参考答案或评分规则。

## 8. Patch 格式与应用

新增：

```text
scripts/apply_gaia2_repair_patch.py
```

patch 采用结构化操作，禁止直接覆盖完整任务文件：

```json
{
  "repair_id": "rep-000001",
  "task_id": "...",
  "operations": [
    {
      "op": "replace",
      "path": "/scenario/events/17/arguments/event_id",
      "value": "$result:series_s1_create.event_id"
    },
    {
      "op": "dependency_add",
      "event_id": "series_s1_message",
      "depends_on": "series_s1_event"
    }
  ]
}
```

支持 `add`、`remove`、`replace`、`move`、dependency 变更、stage event 变更、criteria/checks/communicate_info 变更、reference answer/reply 变更。应用前保存 canonical old JSON 和 hash，应用后生成 new JSON 和 hash，所有 patch 必须可反向回滚。

## 9. Base 和变体的修复顺序

### 9.1 Base

1. 确认用户要求和完整证据；
2. 写入 ledger；
3. 生成 typed patch；
4. 应用 patch；
5. 做结构、语义和 reference replay 验证；
6. 标记 fixed；
7. 记录受影响变体。

### 9.2 Repetition

- 继承来源 base 的修复；
- 检查重复表达是否只是强调；
- 不把重复请求误解为重复执行；
- 单独检查指代、索引和 stage checks。

### 9.3 Mind Change

- 继承 base 修复；
- 确认最终值；
- 明确旧值是否撤回；
- 保留没有被撤回的其他要求；
- 确保 reference 参数和依赖使用最终值。

### 9.4 AddTask

- 保留原目标 A；
- 检查新增目标 B 的用户请求、reference events、参数、依赖和答案；
- 不强制 A、B 使用没有用户依据的顺序；
- 确认 B 进入正确 stage、运行视图和评分入口；
- 检查 A、B 的业务冲突。

### 9.5 Modify

- 检查修改前后请求是否绑定同一后台工作；
- 保留未修改的要求；
- 使用 native modify scoring；
- 不恢复原题 ENV、execution 或 state-interaction 分数。

### 9.6 Cancel

- 确认临时任务 B 存在；
- 确认取消语句明确指向 B；
- 保留原任务 A；
- 定义取消前已经完成部分的状态；
- 检查取消对象、剩余工作和 native cancel scoring。

变体不直接修改展开副本。base 修改写入来源任务，变体独有修改写入 `interaction_variants.json`，再通过 `apply_variant()` 重新生成运行视图。

## 10. 四层验证

### 10.1 结构验证

- JSON 和 Pydantic 模型可以加载；
- event ID 唯一；
- 所有 dependency 目标存在；
- 没有依赖循环；
- stage.event_ids 与事件关系一致；
- source/variant hash 正确。

### 10.2 事实和语义验证

- 查询结果能从完整 snapshot 重算；
- 联系人、参与者、地址、时间和对象 ID 有证据；
- reference 没有执行用户未要求的操作；
- reply 不声称尚未完成的动作；
- 后续阶段只引用已经生成的对象或结果；
- 动态对象使用结果引用或稳定业务属性定位。

### 10.3 评分验证

- tools 只比较必要写操作；
- parameters 不强制检查用户没有指定的标题、主题或固定措辞；
- order 只保留业务必要依赖；
- effects 检查真实后置状态；
- ENV assertion 覆盖整题目标；
- AddTask、Modify、Cancel 使用各自的评分语义。

### 10.4 Reference replay

每个公共根因至少选择 2 道应该通过的代表题、2 道应该失败的反例、每种变体 1 道代表题，并覆盖动态 ID、阶段边界和评分规则。无需完整 Agent 评测，但必须验证修改后的 reference 能被评分入口读取。

## 11. 分批实施顺序

### 第 0 批：协议和工具链

- 收回全局 no-transfer 改动；
- 固定 Gaia2-only 提示版本；
- 修复 source mapping；
- 修复 priority 和 severity normalization；
- 增加 queue validator 和基础测试；
- 更新 prompt fixture、版本文档和实施记录。

### 第 1 批：critical/high 确定性错误

- 固定 ID、错误引用、无效依赖和 stage 错配；
- 修复 AddTask 缺失新增 reference；
- 修复 Modify 最终请求和 reference 不一致；
- 修复 Cancel 临时任务或取消对象缺失；
- 修复明确的查询和回复事实错误。

### 第 2 批：公共评分规则

- exact text 检查；
- 固定数组索引；
- 缺失或多余 order；
- 错误 stage criteria；
- 缺失 effects 或 ENV assertion；
- 合法实现路径被唯一 reference 限制。

### 第 3 批：变体传播

- 重新生成 repetition 和 mind change；
- 重新展开 AddTask、Modify、Cancel；
- 更新 interaction variant hash、manifest 和 provenance；
- 运行每类变体的代表性 replay。

### 第 4 批：证据不足和低风险整理

- 补读完整 snapshot；
- 程序重算查询结果；
- 局部回放对象关系和时间；
- 正确项标记 false_positive 或 by_design；
- 无法确认项保留 needs_evidence 或 blocked；
- 最后处理文档、日志和 authoring metadata。

## 12. 运行命令与检查

脚本检查：

```bash
UV_FROZEN=1 .venv/bin/python -m py_compile \
  scripts/review_gaia2_references.py \
  scripts/triage_gaia2_repairs.py \
  scripts/validate_gaia2_repair_queue.py

UV_FROZEN=1 .venv/bin/ruff check \
  scripts/review_gaia2_references.py \
  scripts/triage_gaia2_repairs.py \
  scripts/validate_gaia2_repair_queue.py
```

针对性测试：

```bash
UV_FROZEN=1 .venv/bin/pytest -q \
  tests/test_user.py \
  tests/test_streaming/test_scenario_series.py \
  tests/test_multilingual/test_gaia2_user_guidelines.py \
  tests/test_multilingual/test_transfer_policy.py \
  tests/test_gym/test_user_gym.py
```

Gaia2 数据验证：

```bash
UV_FROZEN=1 .venv/bin/python scripts/validate_gaia2_repair_artifacts.py \
  --root data/tau2/domains/gaia2 \
  --repair-root data/archives/gaia2/reference-repair-v1

UV_FROZEN=1 .venv/bin/tau2 gaia2 verify \
  --reference --workers 16 --seal
```

`gymnasium`、PyAudio 或系统音频库缺失时，应记录为环境阻断，并单独在完整 voice/gym 环境中补跑，不能把 collection error 当成代码通过。

## 13. 发布、回滚与 provenance

修复完成后生成新的 `tasks.json`、`interaction_variants.json`、`split_tasks.json`、`manifest.json`、`provenance.json`、source/variant hash、`repair-summary.md` 和 validation report。

发布版本必须记录 base 数据 hash、variant 数据 hash、runtime digest、scoring code revision、repair ledger hash、validator 版本、生成时间和命令。

原始数据继续保留在既有 archive。回滚只允许通过恢复完整版本目录或反向 patch 完成，不能手工从新旧任务文件中拷贝字段。

## 14. 完成标准

本计划完成必须满足：

1. queue 覆盖完整 1200 道任务；
2. 400 道非 base 任务的 source ID 与 inventory 完全一致；
3. 所有 source hash 和 variant hash 可验证；
4. 每个 fixed finding 有 evidence、patch 和 validation；
5. base 修复能够传播到对应变体；
6. Gaia2 不生成 transfer，非 Gaia2 transfer 行为不变；
7. 公共评分修复有正例、反例和代表性 replay；
8. 任务、变体、manifest、provenance 可以重新生成；
9. `tau2 gaia2 verify --reference --seal` 通过；
10. summary 能区分 fixed、false_positive、needs_evidence、blocked、by_design 和 shared_rule_fix；
11. 原始 archive 未被覆盖；
12. 任意一条修复可以依据 ledger 回滚。

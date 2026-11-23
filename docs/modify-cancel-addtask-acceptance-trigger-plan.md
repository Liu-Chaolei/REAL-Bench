# Modify、Cancel、AddTask 承接触发与任务绑定实施计划

状态：设计方案，待实施。

本文细化 [Modify、Cancel、AddTask 任务变体与指标迁移计划](modify-cancel-addtask-plan.md) 中的运行机制。核心调整是：后续用户请求由真实的后台承接证据触发，不要求后台一定创建新的独立任务；任务绑定和评分单独记录。实现主要放在后台适配、动态指标运行时和证据记录中，不把新的触发判断写进正式评测系统，不增加重型验收流程。

## 1. 目标与边界

三类变体共用一套“请求交付 → 后台承接 → 后续请求”的机制：

| 变体 | 承接后的动作 | 是否要求新建后台任务 |
| --- | --- | --- |
| AddTask | A 被承接后，发送追加任务 B | 不要求 |
| Modify | A 被承接后，发送修改请求 | 不要求 |
| Cancel | A 被承接后发送临时 B；B 被承接后发送取消 B | 不要求 |

必须保持以下边界：

1. 用户请求实际交付、后台承接、后台操作返回、Agent 回复分别记录。
2. 没有独立后台任务时，不替 Agent 拆分 A、B。
3. 后续请求可以在任务合并时继续交付，但目标绑定状态要保留为 `merged` 或 `unbound`。
4. 只有明确绑定到目标且返回满足条件时，才给 `native_result_reward` 成功。
5. 不使用运行时 LLM 判断 objective 是否与用户文本语义相同。
6. 不增加运行前全题审查、语义复审 Agent、自动修复、失败重跑或双重评分。

## 2. 现有问题

当前绑定器主要按以下条件工作：

1. 优先使用 `request_id` 或已有 ID 关系。
2. 否则等待 `task_created`。
3. 再要求后台 objective 包含 `primary_hint` 或 `target_hint`。

这会产生三个问题：

- 后台 objective 可能是 Agent 改写后的内容，和题目短语语义相同但字面不同，导致 AddTask 不能绑定。
- Cancel 把 B 合并进 A 时不会产生新的 `task_created`，旧逻辑因此连取消追问都不发送。
- `native_operation_started` 与 `native_operation_completed` 的证据边界如果混淆，开始事件的占位结果可能被错误用于评分。

## 3. 核心事实模型

运行时增加一组共用请求关系和承接投影。字段应扩展现有 evidence，不建立每个指标独立的解析器。

```json
{
  "request_id": "variant_id:temporary",
  "phase": "initial|temporary|followup",
  "variant_type": "addtask|modify|cancel",
  "role": "primary|temporary",
  "previous_request_id": "...",
  "delivered_at": 1728976326.6,
  "acceptance": {
    "status": "accepted|unconfirmed",
    "accepted_at": 1728976331.1,
    "event_id": "...",
    "kind": "task_created|operation_accepted|confirmed_update|state_observed",
    "backend_task_id": "..."
  },
  "binding": {
    "status": "bound|merged|ambiguous|unbound",
    "backend_task_id": "...",
    "independent": true,
    "reason": "..."
  }
}
```

`acceptance.status` 表示后台是否承接了这条请求；`binding.status` 表示是否能把请求绑定到一个可评分的后台目标。两者不能合并成一个布尔值。

### 3.1 承接证据等级

按以下优先级归一化后台事件：

1. `task_created`，带真实后台任务 ID。
2. `native_operation_completed` 返回 `accepted=True`，操作为 `modify` 或 `addtask`，带任务 ID。
3. `confirmed_updates` 明确列出该请求或更新。
4. 已有后台任务的状态事件或查询返回 `running`、`queued` 等状态，且任务 ID 唯一。

以下事件不能单独证明承接：

- `native_operation_started`；
- 只有用户请求、没有后台事件；
- Agent 自己口头说“已提交”；
- 后台 objective 与用户文本的相似度。

开始事件可以记录，但 `result` 必须为空；完成事件保留原始返回的 `accepted`、`effect`、`error` 和任务 ID。

## 4. 统一运行状态机

```text
WAIT_INITIAL_ACCEPTANCE
  └─ accepted → INITIAL_ACCEPTED
                    └─ queue followup → FOLLOWUP_QUEUED
                                           └─ delivered → FOLLOWUP_DELIVERED
                                                          └─ accepted → FOLLOWUP_ACCEPTED
```

没有承接证据时，状态保持 `WAIT_INITIAL_ACCEPTANCE`，在结束时记录 `not_reached` 或 `acceptance_unconfirmed`。不重新抽题、不改写请求、不把用户交付自动当作后台成功。

### 4.1 AddTask

```text
A 用户请求交付
→ A 承接证据出现
→ 发送 B
→ 记录 B 的承接证据和绑定状态
```

B 可以被后台创建为独立任务，也可以通过已有任务的 `modify`／`addtask` 接收。两种情况都允许 B 交付；独立性只影响绑定和后续诊断，不阻止用户请求发送。

AddTask 的 `reference_events` 和 `reference_answer` 是离线参考差异，不能被运行时当成 Agent 已经执行的事实，也不能自动注入为后台操作。

### 4.2 Modify

```text
A 用户请求交付
→ A 承接证据出现
→ 发送修改请求
→ 记录修改返回
```

修改应优先绑定到 A 的后台任务。Agent 如果创建替代任务，保留真实事件，但不把替代任务自动算作原生 Modify 成功。

### 4.3 Cancel

```text
A 用户请求交付
→ A 承接证据出现
→ 发送临时 B
→ B 承接证据出现
→ 发送取消 B
→ 记录取消对象和返回
```

B 的承接不要求新的 `task_created`：

- B 新建独立任务：`binding.independent=true`，目标为 B。
- B 通过更新 A 被接受：`binding.status=merged`，目标任务为 A，`independent=false`。
- 没有可靠任务 ID：`binding.status=unbound`。

无论 B 是独立还是合并，只要出现真实承接证据，都发送取消追问。之后：

- 取消独立 B，返回 `effect="cancelled"`：native 结果成功。
- 取消合并后的 A：记录目标错误，不给 B 的取消成功分。
- 没有目标可绑定：取消请求可以已交付，但 native 结果为未绑定。

## 5. 任务绑定规则

绑定不使用运行时语义模型。按以下顺序选择：

1. `request_id`、`call_id`、真实 `backend_task_id` 的直接关系。
2. 同一 session 下，用户请求交付后的 `task_created` 或 accepted operation。
3. 当前阶段、承接时间窗口、父任务关系和已有任务状态。
4. 只有一个候选时绑定；多个候选时标记 `ambiguous`。
5. `primary_hint`、`target_hint` 只作为日志和低优先级辅助信息，不能成为唯一触发条件。

时间窗口按请求交付划分：

- A 的候选事件：A 交付后、变体后续请求排队前出现的承接事件。
- B 的候选事件：B 交付后、取消追问排队前出现的承接事件。

同一任务 ID 可以分别记录 A 和 B 的承接，但 B 的 `independent` 必须为 `false`。不能把同一任务 ID 自动拆成两个后台任务。

## 6. 运行时接线

### 6.1 事件适配

修改 `QwenEventAdapter` 和共用后台事件模型：

- `native_operation_started`：记录调用、任务 ID、参数，`result=None`。
- `native_operation_completed`：使用原始返回生成 `accepted`、`effect`、`error`、`result_available`。
- 统一保存 `request_id`、`call_id`、`session_id`、`backend_task_id`。
- 同一调用的开始和返回通过 `call_id` 合并，但不删除原始事件。

适配器只做字段归一化，不判断题目语义，不调用模型。

### 6.2 变体运行时

在 `InteractionVariantRuntime` 中增加：

- `_record_acceptance(event)`：从事件投影承接事实。
- `_bind_candidate(event, request)`：按 ID、时间和唯一候选绑定。
- `_can_trigger_followup(request)`：只判断上一请求是否有承接证据。
- `_queue_followup(phase)`：排队用户请求并写入请求关系。
- `_finish_binding(reason)`：记录 `bound`、`merged`、`ambiguous` 或 `unbound`。

`advance()` 不再等待 `task_created` 作为唯一触发条件：

- AddTask／Modify：初始 A 承接后触发 follow-up。
- Cancel：A 承接后触发 temporary；temporary 承接后触发 cancel follow-up。

### 6.3 阶段交付

变体新增的用户事件继续由变体运行时管理。普通 `ScenarioStages` 只能交付原始阶段请求，不能提前释放变体 follow-up。`managed_user_events` 必须与原始阶段事件集合合并，不能互相覆盖。

请求排队、首次播放、完整交付分别记录。只有完整交付才设置后续请求的 `delivered_at`，不能使用排队时间代替。

## 7. 指标和证据

触发机制不放入环境评分器或普通任务评测入口。活动指标只读取运行时已经保存的事实。

### 7.1 native result

- Modify：目标任务上的 accepted Modify 返回。
- Cancel：目标 B 上 `effect="cancelled"` 的返回。
- AddTask：业务结果由补充后的 ENV／执行指标评价；后台承接与绑定作为证据日志。
- `merged`、`ambiguous`、`unbound` 不能自动转换为成功或失败数值。

### 7.2 reply

保留现有前置条件：native 结果通过且有实际 Agent 回复后，才计算 reply。否则保存 `not_evaluated` 与原因。

### 7.3 诊断输入

需求理解、发言有用性和精简反馈读取：

- 用户请求的 phase 和前后请求关系；
- 实际交付时间；
- 后台承接事件；
- 实际任务 ID、调用 ID 和返回；
- 是否独立、合并或未绑定。

诊断不重新猜测任务关系，也不读取变体参考答案来补写后台事实。

## 8. 失败和边界状态

| 情况 | 后续请求 | 绑定状态 | 评分含义 |
| --- | --- | --- | --- |
| 用户交付但后台无承接 | 不发送 | unbound / unconfirmed | 不纳入成功 |
| 后台承接但没有唯一任务 ID | 发送 | ambiguous | 保留实际请求，不给目标成功分 |
| B 合并到 A | 发送 Cancel | merged | 可评估是否取消错目标 |
| B 独立创建 | 发送 Cancel | bound/independent | 可评估取消 B |
| started 有、completed 无 | 不认作结果 | 未定 | 不评分 |
| completed 返回 accepted=false | 后续按流程决定 | bound/unbound | 记录拒绝，不补成成功 |
| 任务已终态后才出现承接 | 不强行追问 | unbound | 记录错过机会 |

## 9. 轻量测试

只增加机制单元测试和小型人工事件测试，不加入正式评测主流程：

1. `task_created` 承接 A 后可以触发 AddTask。
2. 已有任务的 accepted Modify／AddTask 后可以触发 AddTask。
3. Cancel 的 B 通过更新 A 被接受时，取消追问仍然交付。
4. Cancel 的 B 独立创建时，取消追问绑定 B。
5. B 合并到 A 时，取消 A 不被判作取消 B 成功。
6. 多候选任务时可以触发，但绑定状态为 `ambiguous`。
7. 没有承接证据时不发送后续请求。
8. started 事件不会产生 native 成功分。
9. 同一调用的 started 和 completed 不会计成两次操作。
10. 原始任务和 AddTask 的业务评分选择不受 Modify／Cancel 配置影响。

不增加运行前任务审查、每轮自检、额外 LLM 裁判、自动重生成、失败重跑或全量参考执行。

## 10. 实施顺序

1. 扩展请求承接和绑定证据字段。
2. 修正 Qwen 事件适配，分开 started 和 completed。
3. 将 `InteractionVariantRuntime` 改为承接驱动状态机。
4. 修正阶段托管事件的合并逻辑。
5. 接入 AddTask、Modify、Cancel 三类触发路径。
6. 保留现有 native/reply 评分，只替换目标绑定输入。
7. 增加轻量机制测试并运行相关回归。
8. 选择每类少量题目重新试跑，检查实际请求交付和事件绑定。
9. 更新实施文档和试跑报告。

完成标准是：承接证据可独立触发后续请求，合并任务时仍保留真实请求和未绑定状态，独立任务时能正确绑定；不要求所有 Agent 都主动创建独立后台任务，也不要求运行时额外调用语义裁判。

## 11. 实施记录

已完成第一版运行时接线：

- InteractionVariantRuntime 增加承接记录和 binding_status，区分 bound、merged、ambiguous、unbound。
- task_created、状态事件和 accepted 的 native_operation_completed 都可以作为承接证据。
- AddTask 和 Modify 在初始请求承接后触发 follow-up；Cancel 在临时 B 承接后触发取消 follow-up。
- Cancel 的 B 合并到 A 时仍发送取消追问，但保留 temporary=merged，不会把取消 A 计作取消 B 成功。
- Qwen started 事件不再生成伪造结果，native 评分只读取完成返回。
- Gaia2 阶段托管集合改为合并，变体事件不会被原始阶段事件覆盖。

轻量机制回归中，承接触发、独立 B、合并 B、已有任务更新承接、started/completed 事件边界等用例通过。完整诊断回归中有 3 个既有音频集成用例因环境缺少 libasound.so.2 失败，其余 367 个用例通过；这些音频环境失败不属于本机制改动。

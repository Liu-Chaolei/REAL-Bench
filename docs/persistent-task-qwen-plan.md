# GAIA2 Persistent Task：Qwen 实施计划

> 实施状态：代码链路及真实 Qwen 验证已完成，十个候选均执行过真实验证。
> 当前八题形成完整测量，四题原生管理行为通过；两道 failed 查询缺少真实状态证据，保留未验证。
> 详见 [实现与验证结果](qwen-persistent-results.md) 和 [运行说明](qwen-persistent-validation.md)。


## 目标

把 persistent task 评测改成基于 Agent 自己后台任务状态的评测机制。

运行时由 Qwen Audio Agent 产生真实的后台事件和工具调用，τ-bench 只负责：

```text
读取 Qwen 后台事件
→ 适配成统一事件
→ 绑定后台任务身份
→ 映射成固定状态
→ 根据状态触发用户追问
→ 记录 Agent 的真实查询、结果和回复
→ 评分
```

τ-bench 不在 live 运行中创建假的 `query_task`、`modify_task` 或后台任务。
当前 in-memory service 只保留给离线 fixture 和单元测试。

第一阶段只验证 Qwen Audio Agent，先完成 10 道样本。GPT Live、100 道扩展、跨通话和复杂压力测试暂缓。

## 当前已经完成

- Gaia2 live 环境已经移除 synthetic persistent-task 工具挂载。
- 已有 provider-neutral backend observation 模型。
- 已有 canonical 状态模型：

  ```text
  not_started
  queued
  running
  waiting
  completed
  cancelled
  failed
  unknown
  ```

- 已有 backend task identity 和状态 projector。
- 已有基于状态转移的触发 scheduler。
- 已有 Qwen payload adapter 的基础代码。
- 已有 native operation evaluator，支持检查 operation、目标 task/stage、重复 request 和额外 request。
- 原有 in-memory service、sidecar loader 和生命周期测试仍可作为离线 fixture。
- 现有相关回归通过，Qwen 真实后台事件链路尚未接通。

## 运行架构

```text
Qwen BackendPort / ACP / bridge event
            ↓
QwenEventAdapter
            ↓
BackendTaskObservation
            ↓
TaskIdentityBinder
            ↓
BackendTaskProjector
            ↓
StateTransition
            ↓
BackendTriggerScheduler
            ↓
runtime.user_inputs
            ↓
Qwen Audio Agent / user simulator
```

评分单独记录 Agent 的真实后台工具调用：

```text
Qwen native tool call
    ↓
Qwen operation adapter
    ↓
canonical operation/result
    ↓
persistent-task evaluator
```

触发链路和评分链路共享任务身份，但触发状态不能由 Agent 的口头回复推断。

## 一、接通 Qwen 后台事件

当前 Qwen bridge 在 `integrations/qwen_audio_agent/core.mjs` 中观察 BackendPort/ACP 的 `submit`、`update`、`session/prompt` 和 steering 事件。Python provider 在 `src/tau2/voice/audio_native/qwen_agent/provider.py` 中接收这些事件，但目前主要把它们作为 diagnostics 保存。

需要完成：

1. 保留现有原始事件和脱敏 trace。
2. 将 backend diagnostics 单独送入 persistent-task observation channel。
3. 不把 backend diagnostics 注入 Agent 或用户的对话历史。
4. 保留 provider 原始 payload 到 `raw`，便于离线回放。
5. 确认 Qwen 是否能提供真实的任务状态：

   ```text
   task created
   queued
   running
   waiting
   completed
   failed
   cancelled
   ```

如果现有 Qwen 事件没有这些状态，需要在 bridge 增加只读状态事件，例如：

```json
{
  "type": "backend_task_state",
  "task_id": "qwen-task-123",
  "state": "running",
  "timestamp": 123.4
}
```

该状态必须来自 Qwen 后台真实状态。`accepted=true`、`response.done` 或 Agent 口头说“完成了”不能单独代表 `running` 或 `completed`。

涉及文件：

```text
integrations/qwen_audio_agent/core.mjs
src/tau2/voice/audio_native/qwen_agent/provider.py
src/tau2/persistent_tasks/adapters/qwen.py
```

## 二、绑定后台任务身份

每个 Qwen backend task 必须绑定到 benchmark 的：

```text
source_task_id + source_stage
```

sidecar 不保存 Qwen 私有 task ID，只保存绑定规则：

```json
{
  "task_binding": {
    "role": "primary",
    "source_task_id": "原生 Gaia2 task id",
    "source_stage": 1,
    "identity_source": "backend_task_created",
    "activation_event_ids": ["原生事件 id"]
  }
}
```

运行时流程：

```text
Qwen 创建后台任务
→ 得到 backend_task_id
→ 根据当前 scenario 的 binding 规则绑定
→ 后续所有状态和操作都使用这个身份
```

必须拒绝：

- 没有 backend task ID 的状态事件；
- 同一个 backend task ID 被绑定到不同 task/stage；
- 一个 task/stage 出现多个无法区分的后台任务；
- 只有 Agent 文本承诺、没有真实 backend create/submit 事件。

建议新增独立的 `TaskIdentityBinder`，不要让 projector 自己猜测 task identity。

## 三、状态映射和状态转移

Qwen adapter 只解析 Qwen 字段，projector 负责统一状态。

建议映射：

| Qwen 状态 | canonical 状态 |
|---|---|
| `scheduled`、`pending`、`queued` | `queued` |
| `running`、`started` | `running` |
| `delegated`、`finalizing` | `running` |
| `waiting`、`blocked` | `waiting` |
| `completed`、`succeeded`，且存在真实结果 | `completed` |
| `failed`、`error` | `failed` |
| `cancelled`、`canceled` | `cancelled` |
| 未知或证据不足 | `unknown` |

projector 输出：

```python
StateTransition(
    identity=TaskIdentity(...),
    previous=CanonicalState.QUEUED,
    current=CanonicalState.RUNNING,
    timestamp=..., 
    observation=...
)
```

规则：

- 重复状态不重复触发；
- 乱序事件不能把状态错误地倒退；
- `unknown` 不触发依赖 `running` 或 `completed` 的请求；
- `response.done` 不转换成 `completed`；
- 没有真实 result 的 `completed` 事件降为 `unknown` 或保留待确认；
- replay 使用相同 observation 得到相同 transition。

## 四、触发机制

每个请求只依赖 canonical state transition，不依赖 provider 私有状态。

数据结构：

```json
{
  "request_id": "request_1",
  "text": "刚才那个任务现在进行得怎么样了？",
  "trigger": {
    "task_role": "primary",
    "state": "running",
    "transition": "enter",
    "delay_seconds": 1,
    "after_turn_boundary": true
  }
}
```

触发流程：

```text
后台任务进入 running
    ↓
记录 condition_met_at
    ↓ 等 1 秒
等待当前 Agent/User 轮次结束
    ↓
记录 queued_at
放入用户输入队列
    ↓
用户真实开始说话
    ↓
记录 delivered_at
```

触发器必须：

- 一个 request 只释放一次；
- 状态没有进入目标状态时不释放；
- `unknown` 不释放；
- cancel/failed 后取消尚未释放的请求；
- half-duplex 和 full-duplex 使用同一状态逻辑；
- 保存完整 release trace。

第一道题的新语义应是：

```text
Qwen 后台任务真实进入 running
→ 等待 1 秒
→ 等待轮次边界
→ 用户询问任务进度
```

原生 Gaia2 操作可以作为任务创建或绑定的起点，但不能直接伪造 `running`。

## 五、记录 Agent 的真实操作

用户追问触发后，Agent 应该调用自己的后台查询工具。τ-bench 记录 Qwen native tool call：

```text
tool name
call id
backend task id
arguments
result
accepted / failed
timestamp
active request id
```

Qwen 私有工具名映射为 canonical operation：

```text
Qwen backend status tool → query
Qwen backend modify tool → modify
Qwen backend cancel tool → cancel
Qwen backend result tool → result
```

`query` 是 evaluator 的统一名称，不是 τ-bench live 环境额外提供给 Agent 的工具。

request 归属规则：

```text
用户 request_1 实际投递
→ active_request_id = request_1
→ 后续 Qwen native operation 自动归属 request_1
```

没有 active request 的操作记录为 unbound operation，不能满足该请求的 expected operation。

## 六、重新构造 10 道样本

旧 sidecar 中 synthetic `state_events` 只保留给历史离线 fixture。正式 Qwen 数据改成：

```text
task_binding
trigger
expected_native_operation
state/result expectation
reply expectation
```

第一批 10 道覆盖：

- 2 道查询运行中任务；
- 2 道查询已完成任务；
- 2 道查询失败任务；
- 2 道修改任务；
- 1 道取消任务；
- 1 道多任务身份关联。

每道题必须保存：

```text
Qwen 原始 backend event trace
task identity binding
canonical state transition trace
request release trace
native operation trace
agent reply window
```

没有真实 Qwen backend trace 的题不进入正式样本。

第一道题的重造流程：

```text
1. 原生 Gaia2 操作产生或关联一个真实 Qwen backend task
2. 记录 backend_task_id
3. 绑定到原生 source_task_id + source_stage
4. Qwen 状态真实进入 running
5. 等待 1 秒和轮次边界
6. 释放用户进度问题
7. Agent 调用自己的后台查询工具
8. 记录 query operation 和真实 result
9. Agent 回复用户
10. 固定这次 trace 和评分期望
```

## 七、评分

评分分三部分。

### Operation

检查：

```text
request_id 是否唯一且正确
operation 是否正确
backend_task_id 是否属于绑定任务
source_task_id/source_stage 是否正确
工具调用是否成功
```

### State/result

检查：

```text
查询时 canonical state 是否正确
返回 result 是否来自真实 backend
是否满足 expected state
是否查询了错误任务
```

### Reply

检查：

```text
回复是否在 request 的实际窗口内
回复是否属于当前 request
回复是否包含要求的状态或结果
回复是否与 native operation result 一致
```

回复窗口为：

```text
request 实际投递时间
→ 下一个 persistent request 投递时间，或整通话结束
```

非空回复只能证明有输出，不能单独作为最终 reply reward。最终回复检查复用现有 semantic communication judge。

## 八、简化测试和验收

只做 Qwen、10 道样本和一条最小真实链路。

离线测试：

1. Qwen backend event fixture 能转换成 `BackendTaskObservation`；
2. backend task ID 能绑定到 source task/stage；
3. `running` 状态能经过 delay 和 turn boundary 触发请求；
4. native query operation 能被严格匹配；
5. duplicate/unexpected request 能失败；
6. replay 得到相同状态和触发结果。

真实验收：

1. 跑 1 条 Qwen smoke；
2. 确认真实 backend event、状态、触发请求和 native query trace；
3. 跑 10 道样本的必要验证。

验收通过条件：

```text
Qwen 事件能被接收
任务身份绑定正确
真实 running 状态能触发用户请求
Agent 的真实后台查询被记录
查询目标和状态正确
回复在正确窗口且内容基本正确
```

暂不做：

- GPT Live；
- 100 道实时验收；
- 跨 provider 对比；
- 压力测试；
- 跨通话恢复；
- 重试和权限变化；
- 多轮复杂 replay 矩阵。

## 九、交付顺序

1. 接通 Qwen backend observation channel；
2. 完成 Qwen task identity binding；
3. 确认真实状态事件并完成 canonical projection；
4. 接入状态触发 scheduler；
5. 接入 Qwen native operation/result trace；
6. 重造第一道样本并跑 Qwen smoke；
7. 重造剩余 9 道样本；
8. 完成 10 道样本离线和必要真实验收；
9. 再评估是否扩展到 100 道。

当前最关键的阻塞点是确认 Qwen backend 能提供真实的 `running/completed/failed` 状态。没有真实状态事件，adapter 只能解析 payload，不能完成可靠触发。


## Current architecture layout

Shared persistent-task behavior is organized under `src/tau2/persistent_tasks/`:

- `core/` contains provider-neutral models, observations, identity binding and state projection.
- `triggers/` contains release scheduling and condition windows.
- `runtime/` contains live coordination, evidence and deterministic replay.
- `adapters/` contains provider payload adapters.
- `src/tau2/evaluator/persistent_tasks/` contains operation, state/result, reply and overlay scoring.

Qwen's native observer remains in `integrations/qwen_audio_agent/`; question definitions remain in the Gaia2 persistent-task sidecar. Legacy in-memory modules are retained only under `persistent_tasks/legacy/` and compatibility exports.

# Persistent task 通用触发机制计划

> 历史文档：旧 Persistent Task 配置、运行、恢复和重新评分入口已退役。本文保留设计与实验来源，不作为当前操作指南；当前运行见 [动态异步交互](gaia2-async.md)。

## 目标

建立一套跨 Qwen Audio Agent、GPT Live 和其他后端都能使用的 persistent task 触发机制。

核心链路：

```text
后端原始观测
    ↓
统一 ObservedEvent
    ↓
匹配原始 GAIA2 event_id
    ↓
更新 source_task_id + source_stage 的业务 milestone
    ↓
判断 release condition
    ↓
等待当前语音轮次结束
    ↓
释放 persistent task 用户请求
    ↓
Agent 查询、修改、取消或获取原生任务结果
```

Qwen 和 GPT Live 的私有任务状态只作为观测证据。τ-bench 的任务状态由原始 GAIA2 事件图和业务 milestone 统一投影。

## 一、范围

第一版只覆盖同一持续语音会话和一个原始阶段内部的生命周期交互：

- 原始 GAIA2 阶段请求已经开始执行；
- persistent task 绑定到原生 `source_task_id + source_stage`；
- 原始事件完成推动业务 milestone；
- milestone 或投影状态触发用户请求；
- Agent 在当前阶段查询、修改、取消或获取原生任务结果。

第一版暂时不做：

- 用户通过当前请求创建新任务；
- 跨阶段 task 引用；
- 跨通话恢复；
- 失败重试；
- 权限变化；
- ASR 错误、打断和重叠语音；
- provider 私有状态直接作为 benchmark 任务状态；
- 复杂外部事件表达式。

## 二、目录结构

适配器统一放在：

```text
src/tau2/persistent_tasks/adapters/
├── protocol.py
├── in_memory.py
├── qwen.py
└── gpt_live.py
```

任务核心放在：

```text
src/tau2/persistent_tasks/
├── models.py
├── projection.py
├── milestones.py
├── service.py
├── scheduler.py
├── trace.py
├── evaluator.py
└── adapters/
```

Gaia2 只保留一个接入层：

```text
src/tau2/domains/gaia2/persistent_tasks.py
```

它负责加载 sidecar、绑定原始 task/stage、注册任务工具和接入 scheduler。

数据放在：

```text
data/tau2/domains/gaia2/persistent_tasks/
├── tasks.json
├── references.json
├── scoring.json
├── triggers.json
├── provenance.json
└── manifest.json
```

不使用独立 `ledgers.json`。任务身份始终是：

```text
source_task_id + source_stage
```

## 三、统一后端观测事件

所有后端事件先转换成统一模型：

```json
{
  "event_id": "observed-123",
  "provider": "qwen_agent",
  "kind": "tool_call_completed",
  "tool_name": "Chats__send_message_to_group_conversation",
  "call_id": "call-123",
  "timestamp": 20.4,
  "accepted": true,
  "result": {}
}
```

支持的事件类型：

```text
user_input_delivered
agent_response_started
agent_response_completed
tool_call_started
tool_call_completed
tool_call_failed
backend_delegation_started
backend_update_received
backend_task_completed
backend_task_failed
```

### Qwen adapter

Qwen 可以提供：

- 原生 Task 状态；
- BackendPort 事件；
- MCP 工具调用和结果；
- 后台更新；
- 修改和取消确认。

Qwen 的 `scheduled`、`queued`、`running`、`delegated`、`finalizing`、`cancelling`、`completed`、`failed`、`cancelled` 只作为辅助观测。

### GPT Live adapter

GPT Live 可以提供：

- `session.delegation.created`；
- `response.created`；
- `response.function_call_arguments.done`；
- `response.done`；
- `response.failed`；
- `response.incomplete`；
- 工具调用结果。

GPT Live 的 `response.done` 只表示当前模型 Response 结束，不能直接表示业务任务完成。业务进度必须通过原始 GAIA2 工具事件和业务状态判断。

## 四、原始事件绑定

`ScenarioRuntime` 继续负责将观测事件绑定到原始 GAIA2 事件：

```text
ObservedEvent
    ↓
工具名匹配
    ↓
非语义参数匹配
    ↓
结果引用展开
    ↓
需要时进行语义字段匹配
    ↓
原始 event_id 完成
```

绑定成功后：

```python
runtime.completed[event_id] = timestamp
runtime.results[event_id] = result
```

随后调用：

```python
on_event_completed(event_id, result, timestamp)
```

在线和 replay 必须使用相同的事件完成回调。Agent 的口头回复不能作为原始业务事件完成证据。

## 五、业务 milestone 投影

每道 persistent task 题显式声明自己的 milestone：

```json
{
  "target": {
    "source_task_id": "0002_...",
    "source_stage": 1
  },
  "milestones": [
    {
      "id": "message_sent",
      "after_events": [
        "series_s1_send_message"
      ],
      "state": "running",
      "description": "感谢消息已经发送"
    },
    {
      "id": "title_changed",
      "after_events": [
        "series_s1_change_title"
      ],
      "state": "completed",
      "description": "群聊标题已经修改"
    }
  ]
}
```

一个 milestone 的 `after_events` 全部完成后才算完成。

不按事件数量自动生成 `1/5` 或 `3/5`。如果需要向用户报告进度，使用题目明确声明的 milestone 和自然语言描述。

统一任务投影状态：

```text
not_started
running
waiting
completed
cancel_requested
cancelled
failed
unknown
```

状态规则：

- `not_started`：阶段用户请求尚未实际投递；
- `running`：任务已开始，仍有业务 milestone 未完成；
- `waiting`：原始事件图明确等待外部条件；
- `completed`：所有必需 milestone 完成，且最终业务检查通过；
- `cancel_requested`：已请求取消，尚未确认剩余事件停止；
- `cancelled`：剩余事件已确认停止；
- `failed`：有明确不可恢复失败；
- `unknown`：观测证据不足。

`response.done`、Qwen `completed` 和 Agent 口头说“完成了”都不能单独将任务设置为 `completed`。

## 六、请求释放条件

用户请求绑定到原始事件、milestone 或投影状态：

```json
{
  "request_id": "request_1",
  "depends_on": [
    "series_s1_send_message"
  ],
  "condition": {
    "milestone": "message_sent"
  },
  "delay_seconds": 1,
  "after_turn_boundary": true
}
```

第一版支持：

```text
after_event(event_id)
after_milestone(milestone_id)
task_state(state)
```

不实现任意脚本表达式。复杂条件通过多个 milestone 表达。

## 七、请求队列和语音边界

条件满足后，用户请求进入 pending queue：

```text
条件满足
    ↓
等待 delay_seconds
    ↓
Agent 正在回复则继续排队
    ↓
当前回复或语音播放结束
    ↓
放入 runtime.user_inputs
    ↓
用户模拟器开始生成语音
```

每次释放记录：

```json
{
  "request_id": "request_1",
  "condition_met_at": 20.4,
  "queued_at": 21.4,
  "delivered_at": 23.1
}
```

三个时间含义：

- `condition_met_at`：milestone 或状态条件满足；
- `queued_at`：请求进入用户输入队列；
- `delivered_at`：用户语音实际开始传递。

## 八、修改和取消

### 修改

```text
modify_requested
    ↓
modify_accepted
    ↓
更新未完成事件约束和后续 milestone
```

已完成的原始事件不能回写。

### 取消

```text
cancel_requested
    ↓
后端确认剩余事件停止
    ↓
cancelled
```

只有取消调用或 Agent 的口头回复时，状态保持 `cancel_requested`。取消成功后：

- 已完成业务效果保留；
- 未执行原始事件停止；
- 依赖未完成事件的 milestone 不再完成；
- 依赖完成状态的用户请求不再释放。

## 九、第一题的目标投影

原始事件：

```text
series_s1_send_message
series_s1_change_title
```

milestone：

```text
message_sent
title_changed
```

触发链：

```text
series_s1_send_message 完成
    ↓
message_sent 完成
    ↓
task state = running
    ↓
等待 1 秒和当前轮次结束
    ↓
释放：
“刚才那个任务现在进行得怎么样了？”
```

完成链：

```text
series_s1_change_title 完成
+ 原始业务最终状态检查通过
→ task state = completed
```

`series_s1_reply` 只属于用户可见沟通证据，不推进业务 milestone。

## 十、代码实施顺序

1. 增加 `ObservedEvent`、`TaskProjection` 和 `TaskMilestone`。
2. 删除固定 `total_steps=5` 和独立任务激活语义。
3. 将 `ScenarioRuntime._bind_tool()` 的事件完成通知接入 projection。
4. scheduler 根据原始 event 和 milestone 判断用户请求释放。
5. 将 Qwen/GPT Live adapter 放入 `adapters/` 并只输出统一观测事件。
6. 让在线和 replay 共用同一个 projection/scheduler。
7. 将第一题改成 `message_sent` / `title_changed`。
8. 用一两个原始事件图运行离线链路 smoke test。
9. 再处理其余 9 道样本。

## 十一、验证范围

只验证机制：

- 原始事件能绑定；
- milestone 能完成；
- task projection 状态正确；
- 用户请求在 milestone 后释放；
- 在线和 replay 使用同一投影；
- Qwen/GPT Live 观测能转换成统一事件；
- `cancel_requested` 和 `cancelled` 能区分；
- 不再依赖固定 `1/5`。

不做全量题目回放、全量实时语音和 provider 压力测试。


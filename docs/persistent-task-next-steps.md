# GAIA2 persistent task 后续实施计划

## 目标

把当前 10 道 draft 样本和 prototype 代码推进成一套可以接入不同 Audio Agent 后端的 persistent task 评测机制。

核心约束保持不变：

- 原生 GAIA2 的 `source_task_id + source_stage` 是唯一任务身份；
- 不创建独立的 `task_1`、`task_2`，不再使用 setup 账本；
- 触发依据是后端可观测事件经过适配后的统一事件和业务 milestone；
- 用户请求只有在触发条件满足、当前轮次结束后才进入输入队列；
- 数据、后端适配、触发调度和评分彼此独立；
- 第一版只覆盖同一通话、同一原始阶段，不做跨通话、跨阶段、重试、权限变化和 ASR 干扰。

## 当前状态

The live architecture has been reset around real Agent backend state. The
in-memory task service is an offline fixture only; Gaia2 no longer mounts it as
an Agent-facing live toolkit. The next implementation work must connect provider
events to the canonical projection before enabling any persistent-task sidecar.

已经具备的部分：

- sidecar 已移除 `ledgers.json`、`setup` 和独立任务 ID；
- 10 道样本已经绑定原始 GAIA2 task/stage 和原始 event ID；
- `ScenarioRuntime` 已有原始事件完成回调；
- scheduler 已能根据依赖事件、状态和 milestone 释放请求；
- 任务服务、标准工具接口、trace 和轻量 evaluator 已经有基础实现；
- 适配器目录已建立，当前有 in-memory adapter；
- 已有 persistent task 单元测试和 sidecar 结构测试。

仍需完成的核心部分：

- 统一的跨后端 `ObservedEvent` 模型；
- Qwen Audio Agent 和 GPT Live 的观测适配器；
- 从观测事件到 GAIA2 原始事件完成的稳定绑定；
- 不依赖固定 `1/5` 的 milestone 投影；
- scheduler 在真实 half-duplex/full-duplex 输入队列中的完整接入；
- 取消后停止未执行原始事件，以及最终结果投影；
- persistent task evaluator 与现有评测结果的独立组件接入。

## 一、先固定数据契约

### 1. `tasks.json`

每条记录只描述题面和构造标签：

```json
{
  "scenario_id": "pt_0001",
  "source_task_id": "原始 GAIA2 task id",
  "source_stage": 1,
  "source_event_ids": ["原始事件 id"],
  "operation_type": "query",
  "reference_type": "recent_reference",
  "requests": [
    {
      "request_id": "request_1",
      "text": "刚才那个任务现在进行到哪一步了？",
      "operation_type": "query",
      "reference_type": "recent_reference"
    }
  ]
}
```

这里不保存后台 provider 的私有状态，也不保存临时触发时间。

### 2. `triggers.json`

只描述原始事件、milestone 和用户请求之间的关系：

- 哪些原始事件完成后可以推进 milestone；
- milestone 采用什么统一状态投影；
- 哪个条件满足后释放哪个用户请求；
- 是否等待当前轮次边界以及最小延迟。

第一版只支持：原始事件依赖、milestone 依赖、状态条件和轮次边界。

### 3. `references.json` 与 `scoring.json`

`references.json` 保存正确操作和原生目标：

```text
request_id + operation + source_task_id + source_stage
```

`scoring.json` 只保存检查开关和期望目标。普通 GAIA2 的工具调用、回答和原有评分继续由原始任务数据维护。

## 二、实现统一观测事件层

新增 `src/tau2/persistent_tasks/observation.py`，定义 provider 无关的事件模型：

```python
ObservedEvent(
    provider: str,
    kind: str,
    timestamp: float,
    event_id: str | None,
    call_id: str | None,
    tool_name: str | None,
    arguments: dict[str, object],
    result: object,
    accepted: bool | None,
    raw: object,
)
```

事件类型只保留触发所需的最小集合：

- `tool_call_started`；
- `tool_call_completed`；
- `tool_call_failed`；
- `backend_update`；
- `backend_task_completed`；
- `backend_task_failed`；
- `agent_turn_completed`。

适配器可以保留原始 payload 到 `raw`，但 scheduler 和 evaluator 只读取统一字段。

## 三、实现原始事件绑定器

新增 `event_binding.py`，把 `ObservedEvent` 转成 GAIA2 原始事件完成通知。

绑定顺序固定为：

1. provider 事件确认工具调用成功；
2. 用工具名和调用方向筛选候选原始事件；
3. 比较可验证的参数字段；
4. 展开前序事件引用；
5. 结果匹配成功后写入 `runtime.completed[event_id]` 和 `runtime.results[event_id]`；
6. 调用现有 `on_event_completed(event_id, result, timestamp)`。

绑定器不根据 Agent 的自然语言回复推断业务完成，也不把 provider 的 `completed` 状态直接当作 GAIA2 任务完成。

在线运行和 strict replay 都使用同一个绑定器，避免实时路径和回放路径出现两套语义。

## 四、实现通用 milestone 投影

新增 `projection.py` 或 `milestones.py`，负责从已完成的原始 event ID 计算原生 task/stage 的统一视图。

每个 milestone 明确声明：

- `id`；
- `after_events`；
- 业务描述；
- 投影状态。

状态集合采用 provider 无关的有限集合：

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

状态来源优先级：

1. 原始 GAIA2 事件图和任务检查结果；
2. 明确的 backend update；
3. provider 的结束/失败事件作为辅助证据。

以下信息不能单独触发 `completed`：

- GPT Live 的 `response.done`；
- Qwen 的 provider `completed`；
- Agent 口头说“完成了”。

若证据不足，投影保持 `unknown`，不释放依赖 completed 的用户请求。

## 五、实现两个后端适配器

适配器放在：

```text
src/tau2/persistent_tasks/adapters/
├── protocol.py
├── in_memory.py
├── qwen.py
└── gpt_live.py
```

### Qwen adapter

只负责把 Qwen BackendPort/ACP/工具事件转换成 `ObservedEvent`：

- `scheduled`、`queued`、`running`、`delegated`、`finalizing` 映射为观测信息；
- `completed`、`failed`、`cancelled` 作为后端证据；
- 不把这些私有状态直接暴露为 benchmark 状态；
- 保留原始 task ID、call ID 和 payload，便于 trace。

### GPT Live adapter

只负责把 Realtime/Live 事件转换成 `ObservedEvent`：

- response 开始/结束；
- function call 参数完成；
- function call 结果；
- response failed/incomplete；
- delegation 或后台更新（如果后端提供）。

`response.done` 只表示一次模型响应结束，必须继续依赖原始 GAIA2 工具事件或业务检查判断任务进度。

## 六、改造 scheduler 接入点

scheduler 采用以下顺序：

```text
ObservedEvent
→ event binding
→ runtime.completed 更新
→ milestone projection 更新
→ release condition 判断
→ 条件满足后记录 condition_met_at
→ 等当前 Agent 轮次结束
→ 记录 queued_at
→ 放入 runtime.user_inputs
→ 记录 delivered_at
```

需要补齐：

- 事件完成时间与 release delay 的统一时钟；
- 当前轮次结束回调；
- 已入队请求去重；
- full-duplex 下等待当前语音播放/回复结束；
- replay 下按同样顺序恢复 release 记录。

触发机制不主动轮询 provider，也不在阶段初始化时预先发送任务查询。

## 七、完善任务操作和取消语义

标准操作保持：

```text
list_tasks
query_task
modify_task
cancel_task
get_task_result
```

操作目标始终是：

```text
source_task_id + source_stage
```

需要补齐的行为：

- `modify_task` 只能修改尚未执行部分允许修改的字段；
- `cancel_task` 进入 `cancel_requested`，确认剩余原始事件停止后变为 `cancelled`；
- 取消后未执行事件不再触发后续 milestone；
- 已完成业务效果保留；
- `get_task_result` 只对完成且有结果的原生任务开放；
- 其他 task/stage 的状态不受影响。

## 八、接入 evaluator

persistent task evaluator 作为独立 reward component，不改写原有 `ENV_ASSERTION`、`COMMUNICATE` 和 `NL_ASSERTION`。

每条请求检查：

- 操作类型是否正确；
- 目标 task/stage 是否正确；
- 查询返回的统一状态/milestone 是否正确；
- 修改字段和值是否正确；
- 取消目标是否正确；
- 结果是否属于正确原生任务；
- 是否错误创建、复制或操作其他任务；
- 用户回复是否准确。

输入包括：

```text
用户实际请求
+ ObservedEvent/原始事件 trace
+ persistent operation trace
+ milestone projection
+ 最终原生任务视图
+ Agent 用户可见回复
```

## 九、数据扩展顺序

先不扩展到 100 题，按以下顺序稳定机制：

1. 用现有 10 道样本覆盖五类操作和单/多任务；
2. 为 10 道样本补齐 milestone、统一事件和参考 trace；
3. 用 in-memory adapter 完成离线参考执行和 replay；
4. 用一条 Qwen smoke 任务验证观察事件到 release 的链路；
5. 用一条 GPT Live smoke 任务验证同一触发协议；
6. 机制稳定后，再按配额扩展到 100 题：关联 30、查询 20、修改 20、取消 15、结果 15；
7. 扩展时保持每题一个原始阶段、五类原始类别各 20 题和单/多任务 30/70 配额。

## 十、轻量验证和交付顺序

只做必要检查：

1. 模型和 service 的状态转换测试；
2. 统一事件模型和两个 adapter 的解析测试；
3. 原始事件绑定测试；
4. scheduler 的条件、延迟、轮次边界和去重测试；
5. 10 道样本离线参考执行；
6. 10 道样本 strict replay；
7. 一个 Qwen 和一个 GPT Live smoke run；
8. sidecar 的来源、依赖、ID 和配额检查。

不做全量实时语音评测、跨通话测试、多 provider 压力测试或全仓库重型回归。

最终交付：

- 通用 `ObservedEvent` 和 event binding；
- Qwen/GPT Live 适配器；
- milestone 投影和 scheduler 接入；
- 原生 task/stage 操作 trace 与独立评分组件；
- 通过离线和 smoke 验证的 10 道样本；
- 机制稳定后扩展的 100 道正式 sidecar；
- 更新后的触发机制和数据说明文档。

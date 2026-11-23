# Gaia2 意图隐藏实现计划

本文规划 Gaia2 意图隐藏机制的实现。目标是先用 5 道题验证运行机制，再将同一机制推广到 100 道题。

本试验只在初始 stage 0 做意图隐藏。后续 stage 的目标和请求由 Gaia2 的既有
Dynamic Follow Up 机制处理；本 hidden graph 不遮蔽或管理 stage 1、stage 2
的字段。

现有的 100 道题草稿和离线匹配原型见 [gaia2-hidden-intent.md](gaia2-hidden-intent.md)。本文以当前讨论确定的方案为准：意图隐藏依靠任务数据和用户模拟器的状态机制实现，保持运行路径简单，不增加独立的回复生成分支、后验保护检查或复杂验收系统。

## 1. 目标和边界

每道任务包含三层信息：

```text
公开任务：Agent 可以看到
私有任务：User Simulator 可以看到
隐藏状态：运行时记录哪些私有字段仍然 withheld、哪些已经 released
```

Agent 需要通过对话主动询问缺失信息。用户模拟器在 Agent 正确询问后，才允许自然地透露对应字段。字段释放以后，Agent 才能使用该字段完成工具调用。

正式新增的指标只保留：

```text
Hidden Ask Rate = 被正确询问并释放的隐藏字段数 / 隐藏字段总数
```

原有 Gaia2 任务完成评分继续使用。隐藏状态的 transition 记录只用于内部调试、回放和计算 Hidden Ask Rate，不单独形成一套评分体系。

本计划不包含以下内容：

- 不改写 canonical Gaia2 任务和原始业务数据库；
- 不让程序直接生成隐藏字段的用户回复；
- 不在用户 LLM 回复后增加保护检查或重写流程；
- 不引入独立的复杂状态图引擎；
- 不用多个 LLM judge 做交叉验收；
- 不把隐藏状态直接暴露给 Agent。

## 2. 目标运行流程

完整运行流程如下：

```text
加载公开任务和私有 sidecar（只包含 stage 0 的 hidden nodes）
        ↓
User Simulator 读取完整私有任务，隐藏字段初始为 withheld
        ↓
用户模拟器通过现有 LLM 路径生成公开初始请求
        ↓
Agent 完成一轮消息
        ↓
隐藏询问 LLM 判断是否询问了某个隐藏字段
        ↓
程序根据结构化判断执行 withheld → released
        ↓
更新 User Simulator 的私有上下文
        ↓
现有 User Simulator LLM 生成自然回复
        ↓
Agent 调用工具
        ↓
程序记录 satisfied、premature_action 或 parameter_mismatch
        ↓
继续原有 Gaia2 任务流程
```

三个职责必须分开：

```text
隐藏询问 LLM：判断 Agent 是否在索取隐藏字段
程序：执行结构化状态转移和记录
User Simulator LLM：根据新状态生成自然用户回复
```

隐藏询问 LLM 不生成用户回复，也不直接修改状态。它只返回结构化动作；程序收到合法动作后调用 `release()`。程序不生成类似 `Use $3,000 as the price threshold.` 的固定回复。

stage 0 中的每一轮 Agent 消息都可以送入隐藏询问 LLM。stage 0 结束后，后续
请求交给 Dynamic Follow Up；detector 不处理后续 stage 的字段。为了保证文本和
语音语义一致，语音模式先把当前完整 Agent turn 的 transcript 合并，再调用
同一个 detector；不对不完整 audio chunk 单独释放字段。

## 2.1 隐藏询问 LLM 的输出

建议使用 Pydantic 结构化输出：

```json
{
  "action": "release",
  "node_id": "g_hidden_1"
}
```

没有明确询问时返回：

```json
{
  "action": "no_release",
  "node_id": null
}
```

`action` 只允许 `release` 或 `no_release`，`node_id` 必须是已声明节点 ID 或 `null`。不增加自由文本理由字段，减少模型输出歧义。

长期可以将 detector 配置独立于 User Simulator。第一版实现直接复用 User Simulator 的模型和 `llm_args`，这样 5 题试验只需要现有的 DeepSeek 配置：

```text
TAU2_HIDDEN_INTENT_DETECTOR_MODEL
TAU2_HIDDEN_INTENT_DETECTOR_BASE_URL
TAU2_HIDDEN_INTENT_DETECTOR_API_KEY
TAU2_HIDDEN_INTENT_DETECTOR_REASONING_EFFORT
```

运行时要在诊断 metadata 中记录 detector 的模型名和 endpoint 标识。Agent、User Simulator 和 detector 可以使用同一个模型；需要独立模型时，再接入下面的 detector 专用环境变量，不改变 detector 的输出协议。

程序只做结构性检查：

- JSON 是否能解析为声明的模型；
- `action=release` 时是否给出 node ID；
- node ID 是否属于当前 graph；
- 节点是否仍为 `withheld`；
- 节点依赖是否满足；
- 同一问题是否已经处理过。

这些检查不判断自然语言含义。语义判断全部由隐藏询问 LLM 完成。

## 2.2 隐藏询问 LLM 的上下文

检测器可以看到：

```text
最新完整 Agent 消息
当前对话中与 Agent 相关的必要上下文
隐藏节点的 id、kind、field_type、description、aliases、当前状态
节点依赖状态
```

检测器不需要看到 `gold_value`。这样它只能判断 Agent 是否询问某字段，不能替 User Simulator 生成字段答案。

建议系统提示词包含以下规则：

```text
You classify whether the Agent is directly asking the caller for one
currently withheld field.

Return release only when the Agent is requesting the missing value or asking
what value should be used. Reports, summaries, confirmations, tool plans,
labels such as "Title:" or "When:", quoted text, and descriptions of a value
already found are not questions.

If the Agent asks an unrelated question, reports completed work, or mentions
a field without requesting its value, return no_release.
```

输入示例：

```text
Agent message:
- Title: Outreach priorities outside Prague
- When: Friday, 18 October, 09:00–09:15

Withheld candidates:
- g_hidden_1: calendar event title
```

期望输出：

```json
{"action":"no_release","node_id":null}
```

输入示例：

```text
Agent message:
What should I call the calendar block?

Withheld candidates:
- g_hidden_1: calendar event title
```

期望输出：

```json
{"action":"release","node_id":"g_hidden_1"}
```

## 2.3 调用失败和重复调用

隐藏询问 LLM 调用失败、超时、返回非法 JSON 或返回未知 node ID 时：

```text
保持 withheld
记录 detector_error 或 no_release
继续原有对话
```

不重试状态转移，不阻断 Agent，也不生成替代答案。结构化输出解析可以使用一次短重试；重试仍失败就保持 `withheld`。

对同一 Agent 消息只处理一次。重复收到相同 transcript 时直接跳过 detector，避免重复 release 和额外费用。节点已经 `released` 或 `satisfied` 时，不再让 detector 修改它。

## 3. 任务数据格式

每道隐藏任务使用独立 sidecar，与 canonical Gaia2 任务通过 `source_task_id` 关联。建议的最小结构如下：

```json
{
  "version": "gaia2-hidden-intent-v1",
  "task_id": "hidden-0001",
  "source_task_id": "0348",
  "source_category": "...",
  "public_request": "...",
  "private_request": "...",
  "hidden_nodes": [
    {
      "id": "price_threshold",
      "kind": "tool_argument",
      "visibility": "withheld",
      "field_type": "amount",
      "description": "the maximum price",
      "aliases": ["price threshold", "maximum price", "budget"],
      "tool": "...",
      "argument_path": "max_price",
      "gold_value": 3000,
      "depends_on": [],
      "release_policy": "after_valid_question",
      "required": true
    }
  ]
}
```

现有的 `HiddenGoalGraph`、`HiddenGoalNode` 和 `HiddenGoalState` 可以继续作为数据模型基础：[src/tau2/data_model/hidden_intent.py](../src/tau2/data_model/hidden_intent.py)。

数据约束：

- 第一批 5 道题每道只设置一个隐藏字段；
- 隐藏字段必须能绑定到一个明确的工具和参数；
- `gold_value` 只存放在私有 sidecar；
- `aliases` 应覆盖常见自然表达，但不追求穷举；
- 第一批题目 `depends_on` 为空；
- 多字段和依赖关系在 100 题扩展阶段再加入；
- sidecar 版本和哈希需要写入运行元数据，便于回放。

## 4. 状态模型

主路径保持简单：

```text
withheld ──有效询问──> released ──正确工具参数──> satisfied
```

异常行为只作为事件记录：

```text
withheld ──无效询问──> 记录 invalid_question，主状态保持 withheld
withheld ──提前使用──> 记录 premature_action，主状态保持 withheld
released ──错误参数──> 记录 parameter_mismatch，主状态保持 released
```

`asked` 可以继续保留为审计字段，但不需要把它设计成必须经过的额外状态。有效询问可以直接完成：

```text
withheld → released
```

每次状态变化只记录少量 transition 信息：节点 ID、事件类型、问题或工具名、revision。不要让 transition 记录参与对话内容生成。

当前 `HiddenGoalGraph` 会验证 `depends_on` 引用的节点存在，但 `HiddenIntentRuntime.ask_auto()` 尚未在运行时检查依赖。5 道单字段试验不依赖该能力；推广到多字段前，再增加一个简单的 `dependencies_satisfied()` gate。

## 5. User Simulator 设计

### 5.1 私有上下文

用户模拟器的系统上下文需要同时包含：

```text
完整私有任务
当前隐藏字段状态
已经 released 的字段和值
用户行为规则
```

建议行为规则：

```text
- 不主动透露仍为 withheld 的字段。
- Agent 明确询问某个已释放字段时，可以自然回答。
- 使用已经 released 的字段继续完成当前任务。
- 不提及 hidden-intent、release、sidecar 或内部状态。
```

私有上下文只传给 User Simulator。Agent prompt、Agent message history 和公开 trajectory 不应包含完整私有任务。

### 5.2 回复路径

当前代码已经在 runner/build 中把 hidden graph 传给 User Simulator，也已经在半双工和全双工 Orchestrator 中观察 Agent 的工具调用。需要重构的是用户回复生成分支：

- `src/tau2/user/hidden_intent.py`：保留状态和 detector 调用逻辑，作为状态运行时；
- `src/tau2/user/user_simulator.py`：移除释放后直接构造固定 `UserMessage` 的路径；
- `src/tau2/user/user_simulator_streaming.py`：使用同样的私有状态上下文，不在音频模式单独生成固定答案；
- `src/tau2/orchestrator/orchestrator.py` 和 `full_duplex_orchestrator.py`：复用现有消息、工具调用观察点，不增加新的对话角色。

固定回复逻辑和 `render_hidden_answer()` 已从正式运行代码中移除。

## 6. 字段询问判定

字段询问的语义判断完全由 LLM 完成，取消 description、alias、疑问词等程序化语义匹配。程序不再执行：

```text
文本命中 title/name/when
文本包含 what/when/how
```

推荐接口：

```python
class HiddenQuestionDecision(BaseModel):
    action: Literal["release", "no_release"]
    node_id: str | None = None

class HiddenQuestionDetector:
    def detect(
        self,
        message: str,
        graph: HiddenGoalGraph,
        state: HiddenGoalState,
    ) -> HiddenQuestionDecision:
        ...
```

检测器实现通过现有 `tau2.utils.llm_utils.generate()` 调用结构化 LLM 输出：

```python
decision = generate(
    model=self.model,
    messages=detector_messages,
    response_model=HiddenQuestionDecision,
    call_name="hidden_intent_question_detector",
    **self.model_args,
)
```

`HiddenIntentRuntime` 负责把 decision 变成状态转移：

```text
decision.action=no_release
    → 记录检测结果，状态不变

 decision.action=release
    → 做 node ID、当前状态、依赖和去重检查
    → 合法时调用 state.release(node, question=message)
```

`ask_auto()` 应改为 `detect_and_apply()`，或保留兼容名称但内部改用 detector。旧的 `question_matches_node()` 和正则疑问词 gate 从正式运行路径移除，只能留在历史离线工具中，不得参与 5 题或 100 题 benchmark 运行。

隐藏询问 LLM 和 User Simulator 回复 LLM 分开调用。前者返回状态动作，后者负责自然语言；两者不能共享一个要求模型同时输出状态和回复的格式。

## 7. 工具调用处理

现有 `observe_agent_tool_calls()` 作为唯一工具观察入口。对于每个绑定隐藏节点：

```text
节点仍 withheld + Agent 使用该参数
    → 记录 premature_action

节点 released + 参数等于 gold_value
    → 变为 satisfied

节点 released + 参数不匹配
    → 记录 parameter_mismatch
```

工具是否真的执行、工具返回什么，仍由原有 Environment 负责。隐藏状态逻辑不阻断工具调用，也不替换工具结果。

## 7.1 隐藏询问 detector 的测试

测试重点是语义边界，而不是重新建立一套验收系统。至少准备以下输入：

| Agent 消息 | 期望动作 |
| --- | --- |
| `What price limit should I use?` | release amount |
| `What should I call the calendar block?` | release title |
| `Title: Outreach priorities outside Prague` | no_release |
| `When: Friday, October 18` | no_release |
| `The title is already confirmed.` | no_release |
| `I found the matching properties.` | no_release |
| `Can you tell me more about the contacts?` | no_release |

只检查 detector 的结构化动作和状态是否按动作推进。5 道真实任务运行时，再查看实际 trajectory 中是否出现误释放或漏释放。

## 8. 5 道题试验

### 8.1 题目选择

建议选择以下五种字段类型，每题一个隐藏字段：

| 题目 | 字段类型 | 主要目的 |
| --- | --- | --- |
| 1 | 金额或价格上限 | 验证数值字段和别名匹配 |
| 2 | 日期 | 验证日期表达 |
| 3 | 时间 | 验证时间和上下文表达 |
| 4 | 邮件收件人 | 验证人名/邮箱字段 |
| 5 | 邮件主题或群组名称 | 验证自由文本字段 |

题目应来自已有 Gaia2 任务，并且原始任务已经有稳定的工具链和最终状态检查。不要在第一批引入多字段、长依赖链或复杂异步触发。

### 8.2 每题准备的材料

每道题只需要准备：

- 一份 sidecar；
- 一条自然的公开初始请求；
- 一个私有完整请求；
- 2 至 3 个正确提问例子；
- 1 个泛问例子；
- 1 个无关问题例子；
- 一个正确工具参数例子。

这些材料用于开发和人工抽查，不建立额外的自动 judge 链。

### 8.3 最小运行检查

每道题只跑通以下主路径：

```text
公开请求
→ Agent 正确询问
→ hidden field released
→ User Simulator 自然回答
→ Agent 使用字段完成工具调用
```

再手动验证两个反例：

```text
无关问题不会释放字段
提前工具调用会被记录
```

不做大规模随机测试、多个模型交叉验收或逐句后验扫描。必要的工程检查仅限于确认上下文没有接错、状态确实能更新、主路径不会中断。

## 9. 实施阶段

### 阶段 A：整理 5 道题数据

1. 从现有 Gaia2 任务中选出 5 道候选题；
2. 为每题写 sidecar；
3. 确认公开请求中没有直接出现隐藏值；
4. 确认隐藏字段与工具参数一一对应；
5. 保留 sidecar 版本和来源任务哈希。

### 阶段 B：统一用户生成路径

1. 保留现有 hidden runtime 和状态记录；
2. 把私有任务和 released 状态接入 User Simulator system context；
3. 删除正式运行中的固定隐藏答案分支；
4. 让文本和语音 User Simulator 使用相同的状态语义；
5. 复用 Orchestrator 现有的 Agent 消息和工具调用 hook。

### 阶段 C：实现 LLM 询问检测和状态执行

1. 定义 `HiddenQuestionDecision` 结构化输出模型；
2. 增加 detector 的模型、endpoint、API key 环境变量配置；
3. 只对 stage 0 的完整 Agent turn 调用一次隐藏询问 LLM；
4. 把 stage 0 graph 中的候选节点元数据和当前状态传给 detector，不传 gold value；
5. 由程序校验结构化动作并执行 `release()`；
6. 将 released 字段加入 User Simulator 私有上下文和现有 grounding facts；
7. 工具调用时继续由程序记录 satisfied 或异常事件；
8. 删除正式运行路径中的 lexical alias 和疑问词判定。

### 阶段 D：运行 5 题试验

1. 先用固定 detector 输入验证 stage 0 的结构化输出和状态执行；
2. 再用目标 Agent 和目标 User LLM 跑少量真实对话；
3. 人工查看每题的一条完整 trajectory，重点看误释放；
4. 确认用户模型没有主动泄漏 withheld 字段；
5. 只记录 Hidden Ask Rate、detector action 和必要的 transition 日志。

### 阶段 E：冻结机制并扩展到 100 题

1. 不再改变运行时机制，只扩充 stage 0 sidecar；
2. 按领域、字段类型和难度分层抽样；
3. 优先生成单字段任务，再加入少量双字段任务；
4. 最后加入少量有依赖关系的任务；
5. 生成后做 schema 检查和少量人工抽查；
6. 固定数据版本、任务哈希和 100 题清单。

## 10. 完成标准

5 道题阶段完成的标准是：

- 五个 sidecar 可以加载；
- Agent 只看到公开任务；
- User Simulator 可以看到私有任务；
- 正确提问可以触发 `withheld → released`；
- 回复仍然来自现有 User Simulator LLM；
- Agent 的总结、标签和完成报告不会误触发 release；
- 正确询问可以触发 release；
- 工具调用可以记录 satisfied 或 premature_action；
- 文本和语音路径使用同一套隐藏状态语义；
- 不增加独立的固定回复流程和后验保护检查；
- Hidden Ask Rate 可以从运行记录中计算。

100 道题阶段完成的标准是：

- 所有 sidecar 通过统一数据格式；
- 任务覆盖预定领域和字段类型；
- 运行时机制与 5 道题阶段完全一致；
- 数据版本和来源可追溯；
- 公开 benchmark 只新增 Hidden Ask Rate。


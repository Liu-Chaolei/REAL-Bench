# Gaia2 动态后续请求调研

## 1. 这次要解决的问题

当前 Gaia2 已经把原始任务扩展成了三阶段请求，但后续请求是在 authoring 阶段生成、审核并写入任务数据的。运行时只会按照 `scenario.stages` 和事件依赖投递这些固定文本。

目标不是实现 JudgeAgent 那种根据模型能力估计来调整题目难度的评测器，也不是让用户模拟器无约束地自由编造业务目标。目标是：

> Agent 正在处理原始 Gaia2 请求时，用户根据已经发生的对话、Agent 的实际回复和当前任务上下文，自然地产生一个相关的新请求或后续子目标。

例如：

- Agent 发现用户要求的航班不可用，用户改问同一天的其他航班；
- Agent 创建了日历事件，用户要求把刚创建的事件改到另一个时间；
- Agent 说消息已经发出，用户要求确认收件人和发送内容；
- Agent 选错了联系人，用户要求纠正刚才的操作。

新请求应当继续属于当前任务框架，能引用已经知道的业务对象或实际结果，并且能够被记录、回放和评分。

## 2. 当前仓库的实现基础

### 2.1 当前文本 benchmark

τ² 的 `text-v1` 是冻结 benchmark：6 种语言 × 3 个域，每个域使用固定的 50 个任务。任务子集、语言绑定和 benchmark manifest 都有固定 hash。文本 benchmark 的动态性来自多轮对话，不来自运行时改变题目集合。

用户模拟器每轮读取对话状态，根据 Agent 的回复生成下一条自然语言。它可以澄清、追问、请求进度、要求重复或对 Agent 的说法作出反应，但当前 grounding 规则要求它保持原始业务目标，不能随意新增收件人、日期、金额、产品或完全无关的业务目标。

相关实现：

- [用户模拟器](../src/tau2/user/user_simulator.py)
- [用户 grounding 与候选恢复](../src/tau2/user/grounding.py)
- [场景运行时](../src/tau2/environment/scenario.py)
- [Gaia2 连续阶段控制器](../src/tau2/orchestrator/scenario_stages.py)
- [Gaia2 连续任务说明](gaia2-series.md)

### 2.2 当前 Gaia2 三阶段任务

Gaia2 的三阶段扩展流程是：

1. 原始请求和后续请求在离线 authoring 阶段生成；
2. 生成结果经过工具执行、结果检查、严格 replay 和独立 review；
3. 接受后的请求写入 `tasks.json` 的 `scenario.stages`；
4. 运行时由 `ScenarioStages` 在用户 STOP 后释放下一个固定请求；
5. 三个阶段共享同一个会话、应用状态和场景时钟，最后统一评分。

因此，当前系统已经具备动态状态、动态环境事件和动态用户自然语言，但后续业务请求本身仍是静态的。

## 3. 外部方案调研

这里需要区分四种不同的“动态”：

1. 只根据 Agent 回复生成下一句自然语言回应；
2. 根据一个预先定义的 conversation plan 推进下一个子目标；
3. 在运行中根据 milestone 或状态投递新的目标修订；
4. 根据当前模型能力生成不同难度的考察题。

本项目需要的是第 2、3 类，部分复用第 1 类的自然语言生成能力，不需要第 4 类。

### 3.1 DeepEval：默认是动态回应，Simulation Graph 可以表达动态请求路径

DeepEval 的 `ConversationSimulator` 接收 `scenario`、`persona` 和 `expected_outcome`。它先生成用户首句，再把每轮 Agent 回复交给用户模拟器，生成下一条“logical user message”，直到达到最大轮数或满足终止条件。[DeepEval Conversation Simulator](https://deepeval.com/docs/conversation-simulator)

默认模式更像：

```text
一个总目标 + 对话历史 + Agent 最新回复
    -> 下一句用户消息
```

它不要求用户模拟器显式输出 `next_goal`，所以可以自然地产生澄清、追问、确认和恢复请求，但后续目标主要隐含在总场景里。

DeepEval 还提供 `SimulationGraph`。每个节点代表用户状态，节点 action 生成下一条用户消息，边描述 Agent 回复满足什么条件时转移到哪个节点。节点可以设置 `terminal=True` 或 `max_visits`，因此可以表达：

```text
初始请求
  ├─ Agent 接受 -> 用户确认
  └─ Agent 拒绝 -> 用户继续提供信息或提出替代方案
```

路由器读取 Agent 回复，在边条件中选择下一状态；节点内部仍然可以用 LLM 把请求自然化。[DeepEval Simulation Graph](https://deepeval.com/docs/conversation-simulator-simulation-graph)

DeepEval 的启示是：

- 请求生成和状态转移可以分开；
- 需要明确的最大轮数、终止条件和分支预算；
- 只用自由生成适合探索，但对需要稳定轨迹的 benchmark 应使用状态图。

### 3.2 ByteChef：目标和 persona 驱动用户模拟器

ByteChef 的多轮场景配置一个 Persona Prompt 和最大轮数。流程是：

1. 可选的 User Message 作为开场；
2. Agent 生成回复；
3. LLM-powered User Simulator 根据 conversation history 和 persona prompt 生成下一条用户消息；
4. 重复直到达到 Max Turns 或模拟器输出 `[CONVERSATION_COMPLETE]`。[ByteChef Evals](https://docs.bytechef.io/platform/automation/build/workflows/ai/agent/evals)

ByteChef 的实现重点是让用户保持一个目标和人格，而不是显式创建新的业务 goal。它适合验证上下文保持、追问处理和多步问题解决，但没有公开一个独立的“下一请求结构化生成”协议。

### 3.3 Google ADK：显式 conversation plan 驱动下一步请求

Google ADK 的 `ConversationScenario` 包含：

- `starting_prompt`：固定首个请求；
- `conversation_plan`：用户需要完成的目标流程；
- `user_persona`：表达方式和反应行为。

官方示例是先要求 Agent 掷一个 20 面骰子，拿到结果后再要求 Agent 判断结果是否为质数。模拟器每轮读取 `conversation_plan` 和 conversation history，动态生成下一条 user prompt。[Google ADK user simulation](https://github.com/google/adk-docs/blob/main/docs/evaluate/user-sim.md)

ADK 的关键分工是：

```text
conversation_plan  -> 下一步做什么
conversation_history -> 当前请求使用哪些事实
user_persona -> 应该怎么说
```

这已经不是单纯的“把上一轮话续写下去”，而是根据计划推进下一个请求。它与 Gaia2 最接近的部分，是可以把三阶段固定请求改成一个高层的请求计划，再由运行时结合实际结果生成当前请求。

ADK 也明确规定了最大交互次数，并提供 `stop_signal`。这说明动态用户请求必须有硬预算，不能让用户模拟器无限扩展任务。

### 3.4 10xGraph：检查剩余目标，再生成推进目标的下一条消息

10xGraph 的 `ConversationScenario` 同时定义 `conversation_plan` 和 `goals`。每轮执行流程是：

1. 发送 `starting_prompt`；
2. Agent 回复；
3. 检查哪些 goals 已经在对话中完成；
4. 如果仍有未完成目标，生成下一条用户消息推进剩余目标；
5. 全部完成或达到 `max_turns` 后结束。[10xGraph user simulation](https://10xgraph.com/docs/testing/user-simulation)

它的抽象可以写成：

```text
当前对话 + 已完成目标集合 + 剩余目标集合
    -> 下一条推进请求
```

这很适合 Gaia2 的“相关新请求”：下一句请求可以是查询结果后的修改、补充约束、失败后的替代方案或对 Agent 声称完成内容的确认。它的限制是 goals 通常仍然在场景中预先定义，运行时主要动态选择和表达下一个目标。

### 3.5 tRPC-Agent-Go：只有初始请求和 conversation plan，也能动态生成后续 turn

tRPC-Agent-Go 支持只提供初始请求和 conversation goal，而不提供完整静态多轮 conversation。运行时把目标 Agent 的最新 `finalResponse` 传给模拟用户，再把模拟用户生成的回复作为下一轮 user turn。没有 `startingPrompt` 时，还可以直接从 `conversationPlan` 生成首句。[tRPC-Agent-Go EvalSet](https://trpc-group.github.io/trpc-agent-go/evaluation/evalset/)

这与 Gaia2 的目标很接近：真实 Agent 回复决定下一轮用户请求的具体内容，但用户模拟器仍然受一个高层计划约束。

### 3.6 MLflow：总目标、persona、guidelines，不显式建模新目标

MLflow 的 `ConversationSimulator` 使用 `goal`、`persona`、`context`、`expectations` 和 `simulation_guidelines`。模拟器持续追求 goal，并可以根据 guidelines 表现出追问、逐步不耐烦、要求具体步骤等行为。[MLflow conversation simulation](https://mlflow.org/blog/multiturn-evaluation)

例如：

```text
goal: 排查 429 rate limit
guideline: 如果回答模糊，逐渐变得不耐烦并要求具体方案
```

MLflow 更偏向“同一总目标下的自然推进”，没有公开独立的 `next_goal` 输出。它还支持从生产 trace 中离线生成新的 test cases，但那是测试场景发现，不是一次运行中的实时请求生成。[MLflow multi-turn evaluation](https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/multi-turn/)

### 3.7 DynaRev：milestone 触发的动态目标修订

DynaRev 是目前与“运行到某个状态后投递新的请求”最接近的 benchmark 设计之一。它研究 web agent 在执行期间遇到用户目标变化时的表现。每个 milestone 有一个 trigger：当环境状态满足条件时，在下一步执行前投递 revision。[DynaRev](https://arxiv.org/abs/2610.40133)

它定义了三种 revision：

```text
additive revision:
    增加一个约束，但保留已有约束

unconditional override:
    用新约束替换原约束

conditional override:
    只有运行时条件满足时才使用新约束
```

例如航班任务可以先搜索航班，之后新增“只考虑直飞”；也可以改成“接受红眼航班”；或者指定“如果白天航班贵超过 150 美元，红眼航班也可以”。

DynaRev 的 revision 文本和触发条件主要在 benchmark authoring 阶段确定，运行时根据 milestone 投递。因此它不是自由生成新业务目标，但它提供了很适合 Gaia2 的请求类型、触发机制和评分思路。

### 3.8 TurnFlow：把 goal shift 和 scope creep 作为多轮挑战

TurnFlow 将多轮任务中的目标变化单独作为 benchmark challenge，包括：

- Scope Creep：用户逐渐扩大任务范围；
- Goal Shift：用户从战略决策转向执行细节；
- Preference Override：后续偏好覆盖前面偏好；
- Constraint Collision：新旧要求冲突。

其中 Goal Shift 的典型结构是：用户先提出一个还没有解决的战略问题，之后提出只有在战略问题解决后才合理的执行请求。Agent 不能机械执行后续请求，而要重新检查累计状态。[TurnFlow](https://arxiv.org/abs/2611.10412)

TurnFlow 的场景由真实对话构造，并使用场景规范、反应式用户模拟器和 response verifier。它说明“下一句请求”本身可以作为被测能力的一部分，而不仅是用户 simulator 的副产品。

### 3.9 传统 task-oriented dialogue：agenda-based user simulator

较早的任务型对话工作使用 agenda-based user simulator。用户不只是维护一段文本，而是维护一个由 goals、requestable slots 和 informable slots 组成的 agenda。用户状态更新后，从 agenda 取出下一个 dialogue act，再把 dialogue act 自然化成话语。[A User Simulator for Task-Completion Dialogues](https://users.cs.duke.edu/~bdhingra/papers/user_sim.pdf)

这个方向对 Gaia2 的启发是：动态后续请求最好先表示成结构化动作，再生成自然语言。例如：

```text
modify_previous_object
object = event_created_by_agent
field = start_time
value = tomorrow 15:00
```

而不是一步让 LLM 直接生成不可验证的自然语言任务。

## 4. 这些工作回答了什么

### 已经有的成熟模式

目前公开实现中，最成熟的是以下模式：

```text
固定初始请求
    ↓
高层 conversation plan / goals / agenda
    ↓
Agent 实际回复和工具结果
    ↓
判断哪些目标已完成、哪些目标仍待推进
    ↓
选择下一类用户动作或 revision
    ↓
生成自然语言请求
```

这些系统通常把“下一步要做什么”和“如何表达”分开。它们普遍使用：

- 高层目标或计划；
- 对话历史；
- 当前 Agent 回复；
- 最大轮数或调用预算；
- 终止信号；
- 有些系统还会使用状态图或 milestone trigger。

### 还没有被充分解决的部分

公开系统普遍没有同时解决以下问题：

1. 新请求可以改变原始业务目标，但必须仍然与原任务有关系；
2. 新请求引用真实工具结果和应用状态；
3. 新请求有确定的业务评分条件；
4. 每次运行的动态请求可以严格 replay；
5. 不同 Agent 产生不同对话路径时，仍能公平比较。

这正是 Gaia2 比普通聊天模拟器更难的地方。

## 5. 对 Gaia2 的直接启示

### 5.1 不应该只做“下一句自然回复”

如果只把当前实现的用户生成 prompt 改成“请根据历史继续说”，生成的内容仍然主要是：

- 对 Agent 回复的确认；
- 澄清问题；
- 要求重复；
- 进度追问。

它不一定稳定地产生一个新的业务请求，也无法明确记录新请求和原任务之间的关系。

### 5.2 更合适的是两阶段生成

建议把动态请求生成拆为两个模型步骤或一个结构化输出步骤：

```text
第一步：选择下一步请求类型/子目标
第二步：把它表达成自然语言
```

请求类型可以先限定为：

```text
continue_same_goal
clarify
add_constraint
modify_previous_result
recover_from_failure
ask_for_status
confirm_result
finish
```

然后输出类似：

```json
{
  "action": "modify_previous_result",
  "parent_entities": ["calendar_event_created_by_agent"],
  "relation": "change_time",
  "request": "刚才那个日历事件可以改到明天下午三点吗？"
}
```

其中 `action`、`parent_entities` 和 `relation` 用于约束与评分，`request` 是真正交给用户模拟器/语音管线的自然语言。

### 5.3 新请求应绑定当前任务中的实体或结果

候选请求至少应满足一项：

- 引用原始请求中的业务对象；
- 引用 Agent 刚才提到的对象、结果或状态；
- 引用实际完成的工具调用；
- 引用当前任务已有的时间、联系人、订单、邮件或日历实体；
- 是当前失败结果的可行替代路径。

以下请求应被拒绝：

```text
原始任务：整理联系人
动态请求：顺便帮我订一张去东京的机票
```

以下请求属于合理的相关后续：

```text
刚才那个联系人已经被删除了吗？
```

```text
你刚才发错人了，请把同样的消息发给正确的联系人。
```

```text
这个日历时间有冲突，那改到明天上午可以吗？
```

### 5.4 触发方式应优先使用 milestone，而不是固定等待

参考 DynaRev，动态请求可以绑定到：

- 某个工具调用完成；
- 某个实体成功创建；
- Agent 报告一个结果；
- 工具调用失败；
- 外部事件到达；
- 某个环境状态或时间条件满足。

触发后再等待现有的轮次/音频边界，避免在 Agent 还没有说完时插入请求。

### 5.5 第一个版本应限制动态范围

建议第一版使用以下约束：

- 每个任务最多一个运行时动态请求；
- 只有在初始请求已经实际送达后才允许生成；
- 不允许连续动态请求；
- 动态请求必须绑定当前任务实体、Agent 回复或工具结果；
- 必须经过结构化相关性和事实检查；
- 允许 `no_new_request`；
- 仍然使用已有 STOP 和总轮数限制；
- 记录生成原因、父 turn、引用的工具调用和实际送达时间。

## 6. 推荐的运行时记录

动态请求不应伪装成预先存在的 `scenario.stages`。建议单独保存：

```json
{
  "dynamic_request_id": "dyn_001",
  "parent_turn": 7,
  "parent_event_id": "series_s1_reply",
  "parent_tool_call_ids": ["call_17"],
  "request_type": "modify_previous_result",
  "relation": "change_time",
  "request_text": "刚才那个日历事件可以改到明天下午三点吗？",
  "generation_prompt_hash": "...",
  "accepted": true,
  "queued_at": 1728975632.2,
  "delivered_at": 1728975634.8
}
```

这样可以区分：

- 原始固定请求；
- authoring 阶段生成并冻结的请求；
- 运行时新生成的请求；
- 普通用户回应；
- 用户 STOP 或终止信号。

## 7. 最终判断

外部工作已经证明，“根据当前对话和剩余目标生成下一条用户请求”是可行的，Google ADK、10xGraph 和 tRPC-Agent-Go 都采用了高层计划/目标驱动的动态用户模拟；DeepEval 的 Simulation Graph 提供了状态图和终止控制；DynaRev 和 TurnFlow 则说明了目标新增、覆盖、条件修订和目标转移可以作为明确的评测挑战。

但外部方案通常不会让模型无约束地创造全新的业务世界。它们更常见的做法是：

```text
预先定义目标框架或请求类型
    +
运行时读取真实对话/状态
    +
模型生成下一步请求文本
```

因此，Gaia2 最适合的方向是 **runtime goal transition / milestone-triggered dynamic user request**：保留原始任务和可评分框架，在运行时根据真实 Agent 交互选择或实例化一个相关的后续请求，再交给现有文本/语音用户模拟器自然表达。


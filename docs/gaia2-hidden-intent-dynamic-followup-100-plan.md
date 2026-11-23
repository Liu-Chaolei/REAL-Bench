# Gaia2 意图隐藏与 Dynamic Follow Up 的 100 题计划

本计划将意图隐藏扩展到 100 道任务。意图隐藏只作用于初始 stage 0；stage 1 和 stage 2 交给现有 Dynamic Follow Up 机制表达。

```text
stage 0：固定公开请求 + 私有 hidden value + Agent 主动询问
stage 1/2：固定业务目标和 source request，运行时由 Dynamic Follow Up 改写表达
```

Dynamic Follow Up 当前是“动态改写预先存在的后续请求”，不是从空白目标生成全新的业务目标。它主要接在 Qwen voice 用户模拟器上，默认关闭；100 题的目标运行必须显式开启。

## 1. 数据选择

从固定的 Gaia2 source population 抽取 100 道题，并记录 source 文件 hash、选择 seed 和 source task ID。建议按类别均衡抽取：

```text
search 20 | execution 20 | ambiguity 20 | adaptability 20 | time 20
```

每道题只选择一个 stage 0 hidden node。第一版不加入多 hidden node 和跨节点依赖。

生成器必须固定：

```text
target_stage = 0
```

stage 0 没有合适字段的任务直接淘汰，不能退回 stage 1 或 stage 2。

适合的隐藏字段包括金额、价格阈值、日期、时间、邮件主题、群聊标题、日历标题和收件人邮箱。字段必须出现在 stage 0、能绑定到具体工具参数、具有明确的 `gold_value`，并能由 User Simulator 根据私有上下文回答。

第一版排除运行时派生的排名、联系人集合、统计值和必须依赖前序结果才能确定的字段。

## 2. Sidecar 结构

每道任务使用独立 sidecar，canonical Gaia2 task 保持原样：

```json
{
  "version": "gaia2-hidden-intent-v2-stage0",
  "task_id": "hidden-0001",
  "source_task_id": "0348",
  "source_category": "execution",
  "target_stage": 0,
  "public_stage0_request": "...",
  "private_stage0_request": "...",
  "hidden_nodes": [
    {
      "id": "g_hidden_1",
      "kind": "tool_argument",
      "visibility": "withheld",
      "field_type": "amount",
      "description": "the minimum price threshold",
      "aliases": ["price threshold", "budget", "minimum price"],
      "tool": "RentAFlat__search_apartments",
      "argument_path": "min_price",
      "gold_value": "$3,000",
      "depends_on": [],
      "release_policy": "after_valid_question",
      "required": true
    }
  ],
  "dynamic_followup": {"stages": [1, 2], "hidden_intent_enabled": false}
}
```

只修改 stage 0 的公开文本和对应用户事件。stage 1、stage 2 的 source request 保持原文，不能全局替换 hidden value。

## 3. 运行时职责

Hidden runtime 只在 stage 0 有效：

```text
stage 0 Agent turn
→ LLM detector 判断是否询问 hidden node
→ 程序执行 withheld → released
→ User Simulator LLM 根据私有状态回答
→ Agent 使用工具
→ 程序记录 satisfied / premature_action
→ stage 0 结束，关闭 hidden detector
→ Dynamic Follow Up 接管 stage 1/2
```

stage 1/2 的表达不进入 hidden graph，也不参与 Hidden Ask Rate。即使后续表达出现 `title`、`name` 或 `time`，也不再触发 stage 0 hidden node。

## 4. LLM detector

detector 每个完整的 stage 0 Agent turn 调用一次。语音模式先合并完整 Agent transcript，不对 audio chunk 单独释放。

输出使用结构化模型：

```json
{"action": "release", "node_id": "g_hidden_1"}
```

或：

```json
{"action": "no_release", "node_id": null}
```

detector 可以看到 Agent 消息、stage 0 候选节点的 ID/类型/描述/aliases 和状态；不读取 `gold_value`。

提示词必须明确：

```text
What should I call the calendar block?
→ release

Title: Outreach priorities outside Prague
When: Friday, October 18
→ no_release

The title is already confirmed.
→ no_release
```

程序只做结构校验：JSON、node ID、节点状态、依赖和去重。语义判断全部由 detector LLM 完成。调用失败、超时或非法 JSON 时保持 `withheld`，记录 `detector_error`，继续对话。

detector 配置可以复用 User Simulator 的 DeepSeek/OpenAI-compatible 模型。运行 metadata 记录 detector 模型、endpoint 标识和 prompt 版本。

## 5. Dynamic Follow Up 配置

目标运行使用：

```json
{
  "dynamic_followup": {
    "enabled": true,
    "stages": [1, 2],
    "max_context_characters": 24000,
    "max_retries": 1
  }
}
```

Dynamic Follow Up 可以读取对话历史、Agent 工具结果、stage 1/2 source request 和已经完成的业务状态。它不能读取仍然 withheld 的 stage 0 `gold_value`，也不能使用未发生的参考结果。

文本运行只验证 stage 0 detector；开启 Qwen voice 的配置才验证后续请求是否被实时改写。

## 6. 生成流程

### 阶段 A：候选筛选

从固定 source population 选取 stage 0 有明确字段的任务，按类别记录选择和淘汰原因。

### 阶段 B：sidecar 生成

为每道题写入 stage 0 public/private request、一个 hidden node、工具参数绑定、source hash 和 `target_stage=0`。

### 阶段 C：轻量数据检查

检查以下内容：

- public stage 0 不包含 `gold_value`；
- private stage 0 包含真实值；
- hidden node 有工具和参数绑定；
- stage 1/2 没有被生成器修改；
- 所有 hidden nodes 的 target stage 都是 0；
- sidecar、graph、任务和 provenance 数量一致。

### 阶段 D：真实试验

先跑 5 题，再抽 20 题，最后跑 100 题。每轮查看少量完整 trajectory，重点确认：

- 正确询问触发 release；
- `Title:`、`When:` 和完成总结不触发 release；
- detector 失败不会中断对话；
- released value 能进入 User Simulator 回复和 grounding facts；
- stage 1/2 由 Dynamic Follow Up 处理，不再触发 hidden detector。

### 阶段 E：冻结

固定 tasks、sidecars、goal graphs、splits、manifest、source hash、detector prompt 版本和 Dynamic Follow Up 配置版本。

## 7. 指标和交付标准

公开新增指标只保留：

```text
Hidden Ask Rate = stage 0 被正确询问并释放的 hidden nodes / stage 0 hidden nodes 总数
```

内部记录 detector action、detector error、release、satisfied、premature_action 和 Dynamic Follow Up event。Dynamic Follow Up 的表达质量不进入 Hidden Ask Rate。

5 题阶段需要满足：

- 5 道任务全部只在 stage 0 隐藏；
- stage 1/2 source request 保持可用；
- 正确询问和总结误触发能够被区分；
- User Simulator 能根据 released value 自然回答；
- Dynamic Follow Up 能继续处理后续阶段。

100 题阶段需要满足：

- 100 个 sidecar 都只包含 stage 0 hidden nodes；
- source task、sidecar 和 graph 可追溯；
- stage 1/2 没有被意外遮蔽；
- detector 和 Dynamic Follow Up 使用固定版本；
- 统一计算 Hidden Ask Rate；
- 原有 Gaia2 业务评分继续使用；
- 公开 benchmark 只新增 Hidden Ask Rate。

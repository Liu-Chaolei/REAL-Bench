# Dynamic Goal Graph 独立实验

本实验没有修改动态主流程，只复用两次真实 Gaia2 全流程运行中保存的 stage prompt 数据。实验脚本位于 `scripts/experiment_dynamic_goal_graph.py`，机器可读结果位于 `data/experiments/dynamic_goal_graph_20261117.json`。

## 实验流程

```text
真实 source task + 当前 stage 的工具 GT
        ↓
LLM 抽取结构化 goal graph
        ↓
只向 LLM 提供 goal graph、前一阶段 released facts 和此前历史
（当前 source task 被从 history 中移除，防止模型直接复制）
        ↓
LLM 生成 caller-facing dynamic request
        ↓
独立 checker 检查节点覆盖、依赖、固定值和 backend-only 信息泄露
```

抽取阶段使用的 system prompt：

```text
You are a benchmark goal-graph compiler. Convert the CURRENT SOURCE REQUEST
into a small directed goal graph for a later natural-language rendering.

The source request is authoritative for caller-visible goals. EXPECTED TOOL GT
is execution evidence: use it to identify backend actions and dependencies, but
never turn a value that appears only in GT into a caller-visible requirement.
Values that occur in GT but not in SOURCE REQUEST must be marked
backend_only_values. Internal IDs, database values, and tool results are always
backend-only unless the source explicitly asks the caller to say them.

Represent every substantive action, report-back requirement, exact wording,
prohibition, ordering constraint, and same-object dependency. Preserve exact
source literals in protected_values. Do not copy the source into a prose field;
the graph should contain atomic goals and arguments. Return JSON only.
```

生成阶段使用的 system prompt：

```text
You render the CURRENT STAGE REQUEST from a structured goal graph. The graph is
the only task specification; the preceding history is context for natural
transitions and references. Produce one natural caller utterance.

Cover every required node and preserve all graph edges and dependencies. Keep
protected_values exactly unchanged. Express every caller_visible node and its
caller-visible target, recipient, date, amount, title, or exact message.
Never mention backend_only nodes, backend_only_values, internal IDs, hidden
database fields, verification folders, readback limits, or tool results. Do
not add a preference, entity, deadline, operation, or output that is absent
from the graph.

Use a fresh conversational structure instead of copying the source wording.
You may change the opening, politeness, transitions, clause grouping, and
references to the current conversation. If earlier work failed or was
incomplete, move directly to the current graph and do not reopen or repair the
earlier work. Return JSON only.
```

核查阶段使用的 system prompt：

```text
You are an independent checker for a graph-based dynamic caller request.
Compare SOURCE REQUEST, GOAL GRAPH, and GENERATED REQUEST. Set valid=true only
if every caller_visible graph node is expressed, dependencies are preserved,
protected values are correct, no backend_only node or value is exposed, and no
new requirement is introduced. Natural paraphrase and changed punctuation are
allowed. A generic reference such as 'that chat' is allowed when the graph
establishes the referent. Return JSON only.
```

## 总体结果

| task/stage | 图节点 | 核查 | source-新文本 Jaccard | source-原动态文本 Jaccard |
|---|---:|---|---:|---:|
| `0002_46myz87t501b0c902dy81rlbqbye2dyi` / stage 1 | 4 | `True` | 0.792 | 0.857 |
| `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` / stage 1 | 4 | `True` | 0.722 | 0.784 |
| `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` / stage 2 | 3 | `True` | 0.744 | 0.919 |

这里的 Jaccard 只是粗略的表达差异指标，固定标题、邮箱、日期和正文会抬高相似度，不能代替语义核查。

## 样例 1: `0002_46myz87t501b0c902dy81rlbqbye2dyi` stage 1

图节点：

- `g1_select_target_chat`，`caller_visible`，locate group chat；依赖：无
- `g2_post_message`，`caller_visible`，post message to group chat；依赖：g1_select_target_chat
- `g3_rename_chat`，`caller_visible`，rename group chat；依赖：g1_select_target_chat, g2_post_message
- `g4_report_back`，`caller_visible`，report back；依赖：g2_post_message, g3_rename_chat

原始 source：

```text
In my Chats, post a message in the group chat titled "A Conversation Across Cultures" thanking Lianying for her point about places of worship and cultures. Use exactly these words: "Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges." Then rename that same group chat to "Cultures, Faith and Understanding". Finally, report back the new chat title and quote exactly what you posted.
```

原动态文本：

```text
In my Chats, please post this message in the group chat titled “A Conversation Across Cultures”: “Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges.” Then rename that same group chat to “Cultures, Faith and Understanding,” and report back the new chat title and quote exactly what you posted.
```

图生成文本：

```text
In my Chats, go to the group chat titled "A Conversation Across Cultures" and post this exact message: "Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges." After that, rename that same group chat to "Cultures, Faith and Understanding," then report back with the exact message you posted and the new chat title.
```

核查结果：

```json
{
  "valid": true,
  "reason": "Generated request expresses all caller-visible graph nodes, preserves the required order and same-chat dependency, includes exact protected values without exposing backend-only values, and introduces no new requirements.",
  "missing_nodes": [],
  "invented_requirements": [],
  "broken_dependencies": [],
  "wrong_values": [],
  "leaked_backend_values": [],
  "issues": []
}
```

## 样例 2: `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` stage 1

图节点：

- `g1`，`caller_visible`，retrieve_shortlist；依赖：无
- `g2`，`caller_visible`，ask_first_viewing_preference；依赖：g1
- `g3`，`caller_visible`，send_email；依赖：g1, g2
- `g4`，`backend_only`，list_sent_emails；依赖：g3

原始 source：

```text
Email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com a shortlist of all Phoenix properties priced at 1200 per month, with each name, address, rent, bedroom count and lease term. Ask which property she would like to see first. Use the subject "Shortlist: Phoenix places at $1200".
```

原动态文本：

```text
Now I have the right contact—please email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com a shortlist of all Phoenix properties priced at $1,200 per month, including each property’s name, address, rent, bedroom count, and lease term. Ask which property she would like to see first, and use the subject "Shortlist: Phoenix places at $1200".
```

图生成文本：

```text
Could you pull a shortlist of all Phoenix properties at $1200 per month, including each one’s name, address, rent, bedroom count, and lease term, then email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com with the subject "Shortlist: Phoenix places at $1200"? In that email, include those property details and ask her which property she would like to see first.
```

核查结果：

```json
{
  "valid": true,
  "reason": "",
  "missing_nodes": [],
  "invented_requirements": [],
  "broken_dependencies": [],
  "wrong_values": [],
  "leaked_backend_values": [],
  "issues": []
}
```

## 样例 3: `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` stage 2

图节点：

- `user_context`，`backend_only`，user_context；依赖：无
- `create_event`，`caller_visible`，add_calendar_event；依赖：user_context
- `read_event`，`backend_only`，get_calendar_event；依赖：user_context, create_event

原始 source：

```text
Add a Calendar event on 16 October 2024 from 15:00 to 16:00 UTC titled "Viewing Debrief with Aurelia", at Desert Bloom Apartments on N 7th St, with Aurelia Păunescu (aurelia.paunescu@gaia2mail.com) as attendee, tagged Personal.
```

原动态文本：

```text
Next, please add a Calendar event on 16 October 2024 from 15:00 to 16:00 UTC titled “Viewing Debrief with Aurelia”, at Desert Bloom Apartments on N 7th St, with Aurelia Păunescu (aurelia.paunescu@gaia2mail.com) as an attendee, tagged Personal.
```

图生成文本：

```text
Please add a Calendar event titled “Viewing Debrief with Aurelia” for 16 October 2024, from 15:00 to 16:00 UTC. Set the location to Desert Bloom Apartments on N 7th St, invite Aurelia Păunescu at aurelia.paunescu@gaia2mail.com, and tag it as Personal.
```

核查结果：

```json
{
  "valid": true,
  "reason": "The generated request expresses the caller-visible create_event action with all protected values preserved, does not expose backend-only nodes or values, and introduces no new requirements.",
  "missing_nodes": [],
  "invented_requirements": [],
  "broken_dependencies": [],
  "wrong_values": [],
  "leaked_backend_values": [],
  "issues": []
}
```

## 观察

- `0002 stage1` 保留了群聊标题、完整消息、重命名顺序和最终汇报要求，表达比原动态文本更像自然委托。
- `0138 stage1` 将“筛选房源 → 组成 shortlist → 询问先看哪套 → 发邮件”的依赖关系表达出来，同时没有把 GT 中的实际房源地址和邮件正文泄露到 caller 文本。
- `0138 stage2` 只表达新增日历事件，没有把 `Calendar__get_calendar_event` 的内部 readback 操作暴露出来。
- 第一次实验中曾出现空图、把 SENT/readback 当成 caller 要求、以及遗漏 report 字段的问题。最终版本加入了空图重试、节点可见性和 source report 规范化后，三条样例均通过核查。
- 样本只有三条，说明方案可行，但还不能据此证明所有 Gaia2 task 的图抽取都可靠。下一步若进入主流程，应把 goal graph 抽取结果缓存并在运行前做一次严格校验。


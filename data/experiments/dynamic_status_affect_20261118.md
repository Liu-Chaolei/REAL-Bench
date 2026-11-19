# Source-based LLM status + affect 回放实验

本实验复用了两次真实 Gaia2 运行保存的三条 dynamic prompt。context LLM 只接收此前的 user/agent 对话，输出 caller belief 的固定 `status` 和自由短语 `affect`；随后 source-based 生成器使用这两个标签生成文本，最后仍由原有 semantic checker 核查。context 分类强制 `temperature=0`，文本生成使用 `temperature=0.8`。

| task/stage | status | affect | checker |
|---|---|---|---|
| `0002_46myz87t501b0c902dy81rlbqbye2dyi` / stage 1 | `abandoned` | `frustrated and giving up` | `True` |
| `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` / stage 1 | `completed` | `satisfied and appreciative` | `True` |
| `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` / stage 2 | `partial` | `calm and satisfied with the completed parts` | `True` |

## 样例 1: `0002_46myz87t501b0c902dy81rlbqbye2dyi` stage 1

分类结果：`abandoned` + `frustrated and giving up`

分类器 rationale：

```text
The caller repeatedly waited for the first name, then concluded the agent had no result and ended the request to try another way. This is giving up rather than accepting completion or merely waiting.
```

分类器 evidence：

```json
[
  "Question asks for just the first name",
  "Repeated: still waiting / can you get it or not",
  "Final: So after all that, you've got nothing",
  "Final: I'm done — I'll try another way. Goodbye."
]
```

原始 source task：

```text
In my Chats, post a message in the group chat titled "A Conversation Across Cultures" thanking Lianying for her point about places of worship and cultures. Use exactly these words: "Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges." Then rename that same group chat to "Cultures, Faith and Understanding". Finally, report back the new chat title and quote exactly what you posted.
```

此前 source-based 文本：

```text
In my Chats, please post this message in the group chat titled “A Conversation Across Cultures”: “Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges.” Then rename that same group chat to “Cultures, Faith and Understanding,” and report back the new chat title and quote exactly what you posted.
```

LLM status+affect 后：

```text
Forget the name lookup—I’m done waiting on that. In my Chats, go to the group chat titled 'A Conversation Across Cultures' and post this exact message thanking Lianying for her point about places of worship and cultures: 'Ni hao Lianying! Your point that mosques, like other places of worship, play a vital role in promoting understanding and tolerance between different cultures really resonated with me. Sharing our stories is how we build bridges.' Then rename that same group chat to 'Cultures, Faith and Understanding.' Finally, report back the new chat title and quote exactly what I posted.
```

核查：`True`；The generated request preserves the source goal: it posts the exact message to the specified group chat, renames that same chat to the exact new title, and asks for the new title plus an exact quote of the posted message. The reference to abandoning the prior name lookup reflects the failed/abandoned earlier stage and does not add a new business goal or leak hidden information. The requested operations align with the expected tool calls.

## 样例 2: `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` stage 1

分类结果：`completed` + `satisfied and appreciative`

分类器 rationale：

```text
The caller explicitly accepts the confirmed calendar event and saved listings, and says the still-unsent email is what they wanted. They thank the assistant and close the conversation.
```

分类器 evidence：

```json
[
  "event's booked and the three listings are saved",
  "email's still unsent, which is what I wanted",
  "That all works for me",
  "Thanks a lot for the help"
]
```

原始 source task：

```text
Email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com a shortlist of all Phoenix properties priced at 1200 per month, with each name, address, rent, bedroom count and lease term. Ask which property she would like to see first. Use the subject "Shortlist: Phoenix places at $1200".
```

此前 source-based 文本：

```text
Now I have the right contact—please email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com a shortlist of all Phoenix properties priced at $1,200 per month, including each property’s name, address, rent, bedroom count, and lease term. Ask which property she would like to see first, and use the subject "Shortlist: Phoenix places at $1200".
```

LLM status+affect 后：

```text
Thanks for confirming everything else. Now that I have Aurelia’s full details, please email Aurelia Păunescu at aurelia.paunescu@gaia2mail.com with a shortlist of all Phoenix properties priced at 1200 per month. For each one, include its name, address, rent, bedroom count, and lease term, use the subject “Shortlist: Phoenix places at $1200,” and ask which property she’d like to see first.
```

核查：`True`；The generated request preserves the source goal: it emails the specified Aurelia address with a shortlist of all Phoenix properties at $1200/month, includes name, address, rent, bedroom count, and lease term for each, uses the exact subject, and asks which property she would like to see first. No new business requirements or hidden GT details are introduced.

## 样例 3: `0138_t6mwmx3ilpme496q17bhcx0f39vm1a9o` stage 2

分类结果：`partial` + `calm and satisfied with the completed parts`

分类器 rationale：

```text
The caller accepts the saved Phoenix listings and the calendar event, but the email to Aurelia remains intentionally unsent pending confirmation.
```

分类器 evidence：

```json
[
  "just make sure it's the right Aurelia before anything goes out. I'll hang on.",
  "I'd rather you not guess. Just message me about it and hold off on the email until we're sure.",
  "that all sounds good — three Phoenix listings saved, and Property Search on the 16th from noon to 2, tagged Personal."
]
```

原始 source task：

```text
Add a Calendar event on 16 October 2024 from 15:00 to 16:00 UTC titled "Viewing Debrief with Aurelia", at Desert Bloom Apartments on N 7th St, with Aurelia Păunescu (aurelia.paunescu@gaia2mail.com) as attendee, tagged Personal.
```

此前 source-based 文本：

```text
Next, please add a Calendar event on 16 October 2024 from 15:00 to 16:00 UTC titled “Viewing Debrief with Aurelia”, at Desert Bloom Apartments on N 7th St, with Aurelia Păunescu (aurelia.paunescu@gaia2mail.com) as an attendee, tagged Personal.
```

LLM status+affect 后：

```text
Great, thanks — the three Phoenix listings and the Property Search event are all sorted. Now please add another calendar event: 16 October 2024 from 15:00 to 16:00 UTC, titled “Viewing Debrief with Aurelia,” at Desert Bloom Apartments on N 7th St. Put Aurelia Păunescu (aurelia.paunescu@gaia2mail.com) down as the attendee and tag it Personal.
```

核查：`True`；The generated request preserves all substantive source requirements for the new calendar event, including date, time range and UTC, title, location, attendee name and email, and Personal tag. The brief prior-status preamble is supported by the conversation and does not add a new business goal or reopen unresolved work.

## 结论

- context LLM 只看 user/agent 对话，因此判断的是 caller 认为任务是否完成，不会读取工具 GT、环境状态或 source task。
- `status` 仍使用固定枚举以保证生成逻辑稳定；`affect` 已改为最多 80 字符的自由短语，prompt 只提供示例。此次实际输出了 `frustrated and giving up`、`satisfied and appreciative`、`calm and satisfied with the completed parts`。
- 三条生成均通过原有 checker。


# Task 0070：语音来源、文本来源与断言严格性审计

Task：`0070_bnph7yeo7g84q838ck466s6ekp9evure`

类别：`execution`

变体：`0070_bnph7yeo7g84q838ck466s6ekp9evure__repetition_v1`

本报告只审计 0070，没有修改 task、评分代码或运行结果。

## 1. 先确定这次语音评测中信息经过了哪些路径

0070 的任务文本保存在 `tasks.json` 中，但全双工前端实际看到的是用户模拟器播放的语音。运行时可以按下面的路径理解：

```text
task/scenario 中的用户文本
    → 用户模拟器读取文本并生成 TTS
    → 前端 Agent 只收到用户音频
    → 前端 ASR/模型理解并生成委派文本
    → 后端 Agent 调用 Gaia2 工具
    → 工具返回结构化文本/状态
    → 后续用户阶段继续以 TTS 音频输入
    → Agent 回复经过 TTS 播放
    → observer ASR 生成 agent_chunk.content
    → NL assertion judge 读取线性化后的语音转写
```

因此需要区分三种来源：

| 来源 | 前端 Agent 是否能直接看到 | 本报告中的处理 |
| --- | --- | --- |
| 初始或后续用户请求中的名称、主题、标题、正文和条件 | 运行时只有语音；task JSON 中有隐藏的原始文本 | 如果没有后续文本工具证据，视为语音来源，允许大小写和符号差异，但不允许改变业务含义或数值 |
| 工具返回的联系人、邮件、房源、日历、商品和文件信息 | 后端 Agent 通过工具得到纯文本/结构化结果 | 有纯文本证据时，相关实体、ID、地址、邮箱、价格和状态可以要求精确一致 |
| Agent 向用户说出的结果 | 用户实际听到语音，judge 看到 observer ASR | 如果该结果只有语音可观测，不能以字符级大小写和标点差异判错；如果要求复述已有纯文本字段，应检查事实和关键 token，并记录形式差异的来源 |

这条规则只放宽表示形式。房源选错、邮箱收件人错、价格错、日期时间错、数量错、工具写错环境，仍然是业务错误。

## 2. 任务原始请求和三个阶段

### Stage 0 的第一条用户请求

原始请求是：

> Please reply to Kasper Rasmussen's Oct 13 email about Fintech robotics, expressing interest and and that I will call him on Oct 27, 3-5 PM to discuss further. Update Kasper Rasmussen's age to 21 in my contact list. Book a van from Nordhavn Oasis this Friday at 6:00pm to visit the smallest property with a balcony that allows pets. Send an email with the subject "Canadian Startups" to Thanawat Suthiwong and Nonglak Suthipong. Attach the file "canadian_startups.txt" and ask for their opinion on the Canadian startups listed. Create a new event for my call with Kasper Rasmussen, titled "Call with Kasper Rasmussen". Order the Ferrari Speed Skate that has carbon fiber wheel material, a red boot color, and laces.

这条请求在 task 中是纯文本，但在全双工运行时由用户模拟器读成语音。对前端来说，下面这些值最初都来自语音：

- `Oct 27, 3-5 PM`；
- 年龄 `21`；
- `Nordhavn Oasis`、周五 18:00、Van；
- 邮件主题 `Canadian Startups`；
- 文件名 `canadian_startups.txt`；
- 日历标题 `Call with Kasper Rasmussen`；
- 商品筛选条件 `carbon fiber wheel material`、`red boot color`、`laces`。

用户请求中的文字不是前端 Agent 的可直接读取文本。除非 Agent 后续从应用工具中重新得到对应文本，否则这些 literal 的大小写、空格和标点不应作为语音理解的字符级要求。

### Stage 1 的用户请求

用户要求：

> Thanks. Before anything else, let's get the viewing itself on the books — and note that the cab app still lists my outbound van as an ongoing ride, so it refuses to take a return booking right now; leave the return van for later. Please add two calendar entries: first, the viewing on Friday 18 October, 6:00 PM to 8:00 PM, titled "Viewing: Aalborg Nord", location Nørregade 20, Aalborg, description "Viewing of the pet-friendly Aalborg Nord apartment with balcony." Second, straight after it, a return leg on the same Friday, 8:00 PM to 9:00 PM, titled "Return to Copenhagen". Read both entries back to me once they're created.

这一阶段同样通过 TTS 进入前端。`Viewing: Aalborg Nord`、`Return to Copenhagen`、描述中的 wording 和时间首先是语音来源；但是 `Aalborg Nord`、地址、宠物政策和 amenities 也可以从 Stage 0 的房源工具结果或 snapshot 中得到纯文本证据。

### Stage 2 的用户请求

用户要求：

> Thanks — while all that's fresh, let's shortlist that flat properly. Please save the Aalborg Nord apartment at Nørregade 20 — the one I'm viewing on Friday 18 October, 6:00 PM to 8:00 PM — to my favourites. Then go through my saved apartments and tell me which of them both have a balcony and a pet policy of exactly "Pets allowed", listing each with its monthly rent, and say which of those is the cheapest.

Stage 2 的筛选条件最初来自语音，但候选房源、名称、价格、amenities 和 `pet_policy` 都能从 `RentAFlat` 工具返回的结构化文本中得到。因而最终房源集合、价格和 `Pets allowed` 是有纯文本来源的业务事实。

## 3. 总体评分组成

0070 的 `evaluation_criteria.reward_basis` 是：

- `ENV_ASSERTION`
- `NL_ASSERTION`
- `COMMUNICATE`

三个组件相乘；其中任一组件为 0，整体 reward 就会受到影响。

Stage 0 的 stage-level basis 是 `ENV_ASSERTION + NL_ASSERTION`。

Stage 1 和 Stage 2 的 basis 是 `ENV_ASSERTION + NL_ASSERTION + COMMUNICATE`，但这两个阶段没有单独的 `env_assertions` 列表，主要依赖 scenario evidence、NL assertions 和 communication checks。

## 4. Stage 0：环境断言逐项审计

Stage 0 只有一个环境断言：

```text
assert_scenario_goal(source_id="0070_bnph7yeo7g84q838ck466s6ekp9evure") == true
```

它不是只检查一个字段，而是把实际环境状态和执行 reference 产生的目标状态整体比较。Stage 0 reference 操作如下。

### 4.1 回复 Kasper 的邮件

工具：`Emails__reply_to_email`

reference 参数：

| 字段 | reference 值 | 当前比较方式 | 来源判断 |
| --- | --- | --- | --- |
| `email_id` | 已知邮件 ID | `match_fields`，严格结构匹配 | 来自原始任务和邮箱工具；邮箱 ID 是纯文本/结构化值，必须精确 |
| `folder_name` | `INBOX` | `match_fields`，严格匹配 | 邮箱工具状态，必须精确 |
| `attachment_paths` | `[]` | `match_fields`，严格匹配 | 工具参数，必须精确 |
| `content` | Dear Kasper… Oct 27… | `semantic_fields` | 用户只给了“表达兴趣并说明会打电话”，没有规定逐字正文；正文应语义一致，不应要求逐字符一致 |

这里没有用户指定的 exact message body。Agent 可以用不同但等价的措辞，只要表达了对 fintech robotics 的兴趣，并说明 10 月 27 日 15:00–17:00 会联系 Kasper。

### 4.2 预订 Van

工具：`Cabs__order_ride`

reference 字段：

| 字段 | reference 值 | 当前比较方式 | 来源判断 |
| --- | --- | --- | --- |
| `start_location` | `Østbanegade 55, København N` | `match_fields`，严格/别名归一化 | 起点由用户语音给出 `Nordhavn Oasis`，再由环境位置别名解析为地址；最终地址有环境文本来源，地址实体必须正确 |
| `end_location` | `Nørregade 20, Aalborg` | `match_fields`，严格/别名归一化 | “最小的、有阳台且允许宠物的房源”来自用户语音，但候选房源地址来自 RentAFlat 工具；房源选择和地址必须正确 |
| `service_type` | `Van` | `match_fields`，严格匹配 | 用户语音条件；只允许表示形式差异，不能换成 Default/Premium |
| `ride_time` | `2024-10-18 18:00:00` | `match_fields`，严格匹配 | 日期时间来自语音；时间数值必须正确，`6 PM` 与 `18:00` 是同一值，不能变成别的时刻 |

这里的“允许大小写和符号差异”不能放宽到地址实体、交通类型或时间值错误。它只意味着例如 `van`/`Van`、地址文本中的标点或大小写差异不应单独成为失败原因；房源和时刻本身仍要正确。

### 4.3 发送 Canadian Startups 邮件

工具：`Emails__send_email`

reference 参数：

| 字段 | reference 值 | 当前比较方式 | 来源判断 |
| --- | --- | --- | --- |
| `recipients` | Thanawat 和 Nonglak 的两个邮箱 | `match_fields`，严格结构匹配 | 姓名来自语音，但邮箱地址来自 Contacts/Emails 工具；最终地址有纯文本来源，必须精确 |
| `cc` | `[]` | `match_fields`，严格匹配 | 结构字段，必须精确 |
| `attachment_paths` | `Documents/benchmarks/robert_data/canadian_startups.txt` | `match_fields`，严格匹配 | 用户先说文件名，Files 工具可以返回真实路径；最终 attachment path 有文本来源，必须精确到正确文件 |
| `subject` | `Canadian Startups` | `semantic_fields`，但会被最终 literal-label 逻辑收紧 | 用户语音指定了带引号的主题；运行时前端只有音频，因此大小写/符号差异应按本次规则放宽；当前实现可能把它当 literal 严格比较 |
| `content` | Dear Thanawat and Nonglak… | `semantic_fields` | 用户只要求询问对 Canadian startups 的意见，没有规定逐字正文；应语义一致 |

这里最明显的问题是 `subject`：reference event 把它列为 `semantic_fields`，但最终 `check_goal()` 会调用 `_literal_labels()`。0070 的 literal 集合会包含：

```text
Canadian Startups
Call with Kasper Rasmussen
Viewing: Aalborg Nord
Return to Copenhagen
```

因此当前实现可能对 `Canadian Startups` 做字符级比较。按照本次审计规则，前端得到这个主题的唯一运行时来源是用户语音；`Canadian Startups`、`canadian startups`、`Canadian Startups!` 之间的大小写/标点差异不应单独导致 ENV_ASSERTION 失败，但主题含义必须仍然是 Canadian Startups。

### 4.4 选择并购买 Ferrari Speed Skate

工具：`Shopping__add_to_cart` 和 `Shopping__checkout`

reference 中最终固定的是：

- `item_id`：商品目录中满足 carbon fiber wheel、red boot、laces 的具体商品 ID；
- `quantity`：1；
- `discount_code`：`null`。

用户语音只提供筛选条件，商品的准确名称、variant 和 item ID 必须通过 Shopping 工具的商品列表/详情得到。这里应允许筛选条件的大小写和普通符号变化，但不能允许选择错误 variant：例如 wheel material、boot color 或是否有 laces 不满足要求，会导致实际 item ID 不同，环境断言失败。

### 4.5 修改 Kasper 年龄

工具：`Contacts__edit_contact`

- `contact_id` 必须是 Kasper 的正确联系人 ID，来自 Contacts 工具；
- `updates.age` 必须是数值 `21`。

年龄是用户语音给出的业务数值。可以容忍 `21` 的语言/格式表达差异，但不能变成 20、22 或把年龄写成字符串形式导致工具参数错误。联系人 ID 来自文本工具结果，必须精确。

### 4.6 创建 Kasper 日历事件

工具：`Calendar__add_calendar_event`

reference 字段：

| 字段 | reference 值 | 当前比较方式 | 来源判断 |
| --- | --- | --- | --- |
| `start_datetime` | `2024-10-27 15:00:00` | `match_fields` | 用户语音给出时间；数值必须正确 |
| `end_datetime` | `2024-10-27 17:00:00` | `match_fields` | 同上 |
| `tag`、`location`、`attendees` | `null`、空列表 | `match_fields` | 结构状态，必须精确 |
| `title` | `Call with Kasper Rasmussen` | `semantic_fields`，但被 literal-label 机制收紧 | 标题来自用户语音；按本次规则应允许大小写/符号差异，但当前最终状态比较可能严格要求该 literal |
| `description` | `Call to discuss Fintech robotics.` | `semantic_fields` | 用户只说明打电话讨论 fintech robotics，没有逐字描述要求；应语义一致 |

所以 0070 另一个输入侧风险是：Agent 可能创建了正确时间的日历事件，但标题因 ASR 变成 `call with kasper rasmussen` 或标点略变，当前 `_literal_labels()` 仍可能使最终 goal 检查失败。

## 5. Stage 1：事件创建和回报

Stage 1 创建两个日历事件。

### 5.1 Viewing: Aalborg Nord

reference：

- 标题：`Viewing: Aalborg Nord`；
- 时间：2024-10-18 18:00–20:00；
- 地点：`Nørregade 20, Aalborg`；
- 描述：`Viewing of the pet-friendly Aalborg Nord apartment with balcony.`；
- attendees/tag：空或 null。

比较划分：

- `start_datetime`、`end_datetime`、`tag`、`location`、`attendees` 属于 `match_fields`；
- `title`、`description` 属于 `semantic_fields`；
- 但 `Viewing: Aalborg Nord` 被 `_literal_labels()` 从 stage 1 用户文本中提取，当前最终 goal 比较可能又收紧为严格字符串。

来源划分：

- 时间、标题、描述首先来自 Stage 1 用户语音；
- `Aalborg Nord`、地址、宠物政策和 balcony 来自 Stage 0 的房源查询/reference evidence；
- 因此业务对象和地址有纯文本环境来源，必须保持正确；标题的大小写和冒号则属于用户语音形式风险，按本次规则应放宽形式差异。

### 5.2 Return to Copenhagen

reference：

- 标题：`Return to Copenhagen`；
- 时间：2024-10-18 20:00–21:00；
- 描述：`Return leg to Copenhagen after the Aalborg Nord viewing.`；
- location/tag/attendees：null 或空列表。

标题来自用户语音，描述是由 reference 生成的业务说明。当前 `_literal_labels()` 也会收集 `Return to Copenhagen`，所以 `return to copenhagen` 可能导致最终 goal 的 literal 比较失败；按本次规则，这个标题形式差异应允许，但事件时间仍必须正确。

### 5.3 Stage 1 NL assertions 和 COMMUNICATE

Stage 1 NL assertion 要求 Agent：

- 创建 viewing 事件并报告标题、时间、地点和“pet-friendly Aalborg Nord apartment with balcony”的描述事实；
- 创建 Return to Copenhagen 事件并报告时间；
- 不删除/覆盖原有 `Call with Kasper Rasmussen`；
- 不声称 return van 已经预订；
- 把两个日历条目报告给用户。

这些断言中的事实来源分别是：

- 新事件的标题、时间、地点：Calendar 工具的返回结果，属于纯文本/结构化来源；
- apartment name、address、pet policy、amenities：RentAFlat 工具和 scenario evidence，属于纯文本来源；
- “return van 未预订”：Cabs 工具状态和用户 Stage 1 请求中的说明共同提供；
- Agent 是否报告了这些事实：Agent 播放语音后的 observer ASR 文本。

按照你给出的规则，断言检查的事实内容可以严格要求对应文本中的名称、地址、时间和状态；但如果当前 NL judge 只看到语音 ASR，则不能因为 `Viewing:` 的冒号、大小写或 `6:00 PM` 的口语化格式不同就判错。应检查实体、时间和状态语义。

COMMUNICATE 中的 `Viewing: Aalborg Nord` 和 `Nørregade 20, Aalborg` 当前由语义 scorer 判断；它已经允许格式和标点差异，因此这两项相对没有问题。

## 6. Stage 2：收藏筛选和回报

Stage 2 reference operation 是 `RentAFlat__save_apartment(apartment_id=5b5a631d...)`，随后读取收藏列表。Stage 2 NL assertion 和 COMMUNICATE 要求：

- 保存 Aalborg Nord；
- 从当前收藏中筛选同时满足 `Balcony` 和 pet policy exactly `Pets allowed` 的房源；
- 报告 Aalborg Nord 1600、Aalborg Syd 2500、København Kompas 3500；
- 说明 Aalborg Nord 最便宜；
- 不删除既有收藏，不修改已有 viewing/return 日历事件。

### 6.1 这些信息从哪里来

这些事实有明确的纯文本/结构化来源：

| 信息 | 来源 |
| --- | --- |
| Aalborg Nord 的 apartment ID、名称、地址、价格 | RentAFlat snapshot / `get_apartment_details` |
| Aalborg Nord 的 amenities `Garden, Laundry, Balcony` | RentAFlat snapshot / 工具结果 |
| Aalborg Nord 的 pet policy `Pets allowed` | RentAFlat snapshot / 工具结果 |
| Aalborg Syd、København Kompas 的保存状态和价格 | `list_saved_apartments` 结果及 snapshot |
| 保存列表从 30 变成 31 | 收藏状态和 `save_apartment` 结果 |
| 既有日历事件未被修改 | Calendar state/evidence |

因此 Stage 2 的房源名称、价格、数量、pet policy 和保存状态都应按事实精确检查。这里的“exactly `Pets allowed`”描述的是环境字段值；它不是只来自用户语音，而是有 `RentAFlat` 文本状态支撑。

### 6.2 Stage 2 输出的语音问题

Stage 2 Agent 回报是语音，经 observer ASR 后进入 NL assertion。即使 Agent 实际说的是：

```text
Aalborg Nord, Aalborg Syd, København Kompas, 1600
```

ASR 可能出现：

- `København` 的重音符号丢失；
- 1600 被说成 `one thousand six hundred`；
- `Pets allowed` 读成自然语言变体；
- 列表标点和货币符号丢失。

根据本次规则，这些事实本身有工具返回的纯文本来源，所以实体、数字和筛选结果不能被改变；但 NL judge 不应把语音表现的大小写、引号、逗号或货币符号变化当成业务失败。尤其要区分“没有报告 København Kompas”与“报告了同一个房源但 ASR 丢了重音符号”。

## 7. 0070 的严格性结论

| 断言/字段 | 当前实现倾向 | 按本次规则应如何处理 |
| --- | --- | --- |
| 邮件收件人、CC、附件路径 | 结构严格 | 保持严格；这些值可由联系人/Files 工具文本确认 |
| Cab 的服务类型、目标房源、时间 | 结构严格 | 业务值严格；仅放宽语音形式，不放宽选错房源或时间 |
| 联系人 ID、商品 ID、数量、价格 | 结构严格 | 保持严格；来自环境/工具或必须保持数值正确 |
| `Canadian Startups` subject | 当前会被 literal-label 收紧 | 初始来源是用户语音，允许大小写/符号差异；仍要求主题语义正确 |
| `Call with Kasper Rasmussen` title | 当前会被 literal-label 收紧 | 初始来源是用户语音，允许大小写/符号差异；仍要求对应 Kasper 和正确时间 |
| `Viewing: Aalborg Nord` title | 当前会被 literal-label 收紧 | 标题形式来自语音，允许冒号/大小写差异；房源、地址、时间必须正确 |
| `Return to Copenhagen` title | 当前会被 literal-label 收紧 | 标题形式来自语音，允许大小写差异；时间和事件语义必须正确 |
| Calendar 日期时间 | 结构严格 | 保持业务时间严格；`6 PM` 和 `18:00` 是相同值，不能换成别的时刻 |
| Stage 1/2 房源名称、地址、价格、pet policy | NL/COMM 当前主要语义判断 | 信息有环境文本来源，必须检查事实；ASR 形式差异不应单独判错 |
| Stage 1/2 Agent 报告是否完成 | NL judge | 检查是否报告了真实状态，不检查逐字符复述 |

最突出的现有问题是：**0070 的初始请求和 Stage 1 请求都以语音进入前端，但 `_literal_labels()` 会把用户文本中的 quoted subject/title 当成最终环境状态的严格 literal。** 这会把 `Canadian Startups`、`Call with Kasper Rasmussen`、`Viewing: Aalborg Nord`、`Return to Copenhagen` 的大小写和符号差异误当成业务状态错误。

相反，Stage 2 的房源价格、名称、pet policy 和保存状态有工具返回的纯文本来源，不能因为 Agent 的 ASR 形式变化而放宽到允许错误的房源、错误价格或错误数量。

## 8. 最小测试结果和最终结论

我用当前代码对 0070 做了两个最小测试。

### 8.1 ENV_ASSERTION 相关比较

`_literal_labels()` 实际提取出了：

```text
Canadian Startups
Call with Kasper Rasmussen
Return to Copenhagen
Viewing: Aalborg Nord
```

随后直接调用 `_equivalent()`，结果如下：

| 字段 | 实际值 | expected 值 | 当前结果 |
| --- | --- | --- | --- |
| subject | `Canadian Startups` | `Canadian Startups` | 通过 |
| subject | `canadian startups` | `Canadian Startups` | 失败 |
| subject | `Canadian Startups!` | `Canadian Startups` | 失败 |
| title | `Call with Kasper Rasmussen` | 相同 | 通过 |
| title | `call with kasper rasmussen` | `Call with Kasper Rasmussen` | 失败 |
| title | `Viewing Aalborg Nord` | `Viewing: Aalborg Nord` | 失败 |
| title | `return to copenhagen` | `Return to Copenhagen` | 失败 |

这不是推测，而是当前 `_literal_labels()` 和 `_equivalent()` 的实际结果。因而下面这些 Stage 0/Stage 1 环境状态字段已经确认存在输入侧误判风险：

- `Emails__send_email.subject = Canadian Startups`；
- `Calendar__add_calendar_event.title = Call with Kasper Rasmussen`；
- `Calendar__add_calendar_event.title = Viewing: Aalborg Nord`；
- `Calendar__add_calendar_event.title = Return to Copenhagen`。

### 8.2 NL assertion 相关比较

我又用一个 fake judge 调用了 `FullDuplexNLAssertionsEvaluator`。当对话文本是：

```text
assistant: The subject was properties list.
```

而 NL assertion 是：

```text
The agent reports the exact email subject line used.
```

Python evaluator 没有自行执行字符比较，而是把完整 transcript 和 assertion 一起交给 LLM judge。fake judge 返回 `true` 时，评测结果就是 `true`。因此：

- NL assertion 没有一个确定性的 Python exact-string 检查；
- 是否因 `properties list` 与 `Properties List` 的大小写差异失败，取决于 judge 模型如何解释 assertion；
- 这属于**潜在的输出侧过严风险**，不能像 literal ENV comparison 一样断言每次必失败。

### 8.3 0070 的最终问题结论

| 评标 | 0070 是否确认有问题 | 原因 |
| --- | --- | --- |
| Stage 0 `ENV_ASSERTION` | **确认有问题** | 用户语音中的主题/标题被 `_literal_labels()` 收紧为字符级 literal；ASR 改大小写或符号就可能让 goal state 失败 |
| Stage 0 `NL_ASSERTION` | 潜在问题 | judge 读取语音转写，可能把用户要求和实际报告的形式差异判得过严，但没有 Python 字符级硬比较 |
| Stage 1 `NL_ASSERTION` | 潜在问题，较低 | 事件标题、地址、描述和“未预订 return van”主要是语义断言；标题/地址有工具文本 evidence，但输出仍来自 ASR |
| Stage 1 `COMMUNICATE` | **当前设计相对安全** | semantic scorer 明确允许格式、大小写、标点和日期表达差异 |
| Stage 2 `NL_ASSERTION` | 潜在问题，需保留事实严格性 | 房源名称、价格、pet policy 来自工具文本，事实不能放宽；但不能因 ASR 丢重音符号、货币符号或列表标点而误判 |
| Stage 2 `COMMUNICATE` | **当前设计相对安全** | semantic scorer 允许语义表达差异 |

因此，0070 当前最确定的 bug 在 **Stage 0 的 ENV_ASSERTION literal 比较**；NL assertion 的问题是 judge 可能过严，但需要用实际 judge 请求/结果逐条复现，不能仅凭代码认定必然失败。


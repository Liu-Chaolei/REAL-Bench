# Agent 用户模拟器调研：角色、知识、合作与语音交互

调研日期：2026-11-22。状态：调研与建议，未改运行代码或评测定义。

## 1. 结论

“打电话给客服的客户”是一种特定领域的角色，不是用户模拟器的通用定义。在航空、零售、信用卡支持中合理；直接用于个人助理的日程、消息、购物和跨应用委托，会额外引入客服机构、主管和账户访问等社会关系假设。

本次核对了 13 个有用户交互或模拟机制的代表性项目，并补充 3 类固定音频对照。证据包含官方提示词、源码、论文和维护方说明。不是穷尽所有工作，也没有把只有静态任务输入的评测算成动态用户模拟器。各项目的具体设定见下文与来源目录。

对当前 Gaia2，建议以“使用个人助理的任务发起者”为基础角色，将任务事实、用户知识、应用接入与授权边界、交流风格、语音行为和结束规则分别表达。首要修正是角色与环境不匹配；不能通过让用户无限授权、提供答案或反复教 Agent 如何调用工具来提高成功率。

0760 的用户表现与当前提示高度一致，但不能据此证明“用户角色导致了 Agent 不委派”。前台没有调用后台是独立的 Agent 行为问题；纠正模拟器能减少不合理对话分支，不保证业务成功。

## 2. 各项目如何设定用户

以下“核对程度”表示本次实际能检查到的材料，不评价项目质量。“真实闭环”指下一轮用户输入依赖当前 Agent 表现，而非仅把预制多轮文本转换为音频。

| 项目 | 模式与核对材料 | 用户角色和主要设定 | 对本项目最有用的启示 |
|---|---|---|---|
| τ-bench | 文本闭环；源码提示 | 通用开头为与 Agent 交互的用户；题目背景主要为航空/零售客户 | 角色来自任务领域，未知信息不能编造，分步披露是特定测量选择 [S1] |
| τ²-bench / τ-Voice | 文本与语音；公开 scaffold、论文 | 客服客户，加任务说明和人设；语音另有打断、噪声、停顿等机制 | 客服设定有原领域依据，语音不必天然等同客服 [S2]、[S3] |
| EVA-Bench | 音频闭环；实际提示文件 | 航空旅客、IT 服务台员工等，目标、硬要求、软偏好、协商和结束条件分开 | 按领域设定身份；合作、升级和退出行为可以显式配置 [S4]、[S5] |
| Gaia2 / ARE | 动态环境；UserProxy 和 AUI 源码、论文 | 中性的任务委托者；具体 benchmark 还采用预设用户事件 | 框架支持 LLM 用户，不代表每个原始场景都由自由客户对话推进 [S6]、[S7]、[S8] |
| ToolSandbox | 文本闭环；实际用户提示和示例 | 委托虚拟助手完成设备任务的用户；已知信息有限，允许必要设置操作 | 用户可以不知道答案，同时合理要求助手查询工具 [S9]、[S10] |
| UserBench | 文本闭环；回复策略提示与环境代码 | 有隐含旅行偏好的用户，偏好逐步披露 | 澄清难度可以来自信息披露规则，而非固定投诉角色 [S11]、[S12] |
| CollabLLM | 文本协作；论文完整提示 | 与 AI 协作完成写作、编程、数学任务的人 | 可表达困惑、给反馈和协作；不是每个人都在向机构客服求助 [S13] |
| Dialogue-SWEBench | 文本闭环；论文与完整用户提示 | 提交软件问题、通过对话与工程师合作的人 | 明确用户与 Agent 各能看到什么、能做什么 [S14] |
| Joi-Bench-Task | 全双工音频；论文机制与配置 | 用助手执行任务，并在指定时机追问、改要求、暂停/恢复播报的人 | 事件控制决定测试什么，LLM 负责把合法行为说自然 [S15] |
| ConvLab / MultiWOZ agenda | 对话行为层；规则模拟器源码 | 预订/查询服务的用户，由目标槽位和待办议程驱动 | 没有自由人设也能模拟用户；目标状态可独立于语言表达 [S16] |
| UserLM-2.0 | 专用文本用户模型；官方模型卡与论文 | 由任务意图条件化的用户，生成首轮、后续回复和结束信号 | 换角色提示只是一个维度，任务忠实度与自然性仍是不同目标 [S17]、[S18] |
| Ultravox 的 VoiceAgentBench | 面向语音产品的文本评测；维护方设计说明 | 每个 scenario 配一组 AgentProfile / UserProfile | 用户身份与被测 Agent 场景配对；未核对到完整公开用户模板 [S19] |
| MINT | 文本多轮反馈；论文 | 给解题 Agent 自然语言反馈的用户代理 | “用户”也可以是反馈提供者；其信息权限不可直接移植到本项目 [S20] |

### 2.1 τ-bench：原始通用提示并没有把所有用户写成客服客户

其 LLM 用户提示开头是：

> You are a user interacting with an agent.

后续要求按当前步骤提供信息、不编造题目外事实、目标满足时停止，并遵循题目中的性格。它的航空/零售环境仍然主要是客服任务，因此“通用表述”和“实际测量领域”应分开理解。[S1]

这说明：可以复用用户模拟框架，同时通过场景定义角色；不必在共享层永久写死一种机构关系。它的逐步披露规则也不等于所有个人助理任务都必须故意藏信息。

### 2.2 τ²-bench / τ-Voice：客服角色合理，但不是跨领域通用角色

公开 voice scaffold 明确为客服来电者，叠加未知信息、信息披露、人设和结束规则。τ-Voice 在此基础上增加 TTS、音频条件与双向说话机制；这些语音能力与“客服客户”这个身份并不是同一个概念。[S2]、[S3]

对我们而言，可以保留语音合成、监听、轮换与打断的基础设施，再为 Gaia2 定义匹配个人助理的用户角色。无需为了更换角色重做语音框架。

### 2.3 EVA-Bench：领域角色、需求优先级和升级行为分开设置

实际提示文件分别设定航空旅客与 IT 服务台来电员工，并将必须满足的要求、可让步偏好、支持信息、协商行为和结束条件作为独立输入。它还区分“同意执行”和“确认已执行”，并有按场景限制转人工的规则。[S4]

维护方的配置说明提供了停滞若干轮、目标无法完成等结束路径。[S5] 可借鉴的是显式表达合作与结束语义，而不是照搬其逐轮验证、重生成成本，或把“找主管”移植到个人助手场景。

本次还注意到 ITSM 模板的一些段落仍带航空用语。这是公开模板也可能残留领域措辞的例子，说明迁移角色时应查看实际组装的完整提示，而非只看标题。

### 2.4 Gaia2 / ARE：默认代理是任务委托者，原始场景还包含脚本事件

`UserProxyLLM` 的默认提示是：

> You are a Human user interacting with an AI agent that is trying to solve a task you gave them.

它用消息历史生成用户回复；AUI 支持将这个代理接入 Agent–用户消息交换。[S6–S7] 这层代码没有默认要求投诉、找主管或假装不知道助手能操作自己的应用。

但 ARE 是框架，不能把这个默认类等同于 Gaia2 全部实际用户机制。原论文的场景由事件和依赖组织，其中 Ambiguity 注释规则明确有不提供用户澄清后续消息的单轮场景。[S8] 我们增加的实时语音自由对话与 dynamic follow-up 是新的交互条件，需要独立说明其角色与行为假设。

### 2.5 ToolSandbox：用户不知道答案，不代表只能向客服投诉

源码用户提示将模拟者设为任务发起方，限制只能使用给定信息、使用简短语言。任务做不了时，可以补充已知信息；没有可补充信息时，可以让助手自行通过工具寻找。它还明确允许完成任务所需的低电量、网络和定位设置变更，并设有失败尝试后的结束规则。[S9]

这是本次最贴近 Gaia2 的参照：环境能做什么、用户愿意允许什么、用户知道哪些事实，是三件不同的事。不能从“用户不知道联系人姓氏”推出“用户也不知道自己的助手接入了通讯录”。该项目同样存在刻意留空工具或信息的测试，不能反过来强迫所有请求都可完成。[S10]

### 2.6 UserBench：把信息披露当作明确的测试变量

旅行用户有预设偏好。实现会根据 Agent 是否问到了已有偏好、是否问得过于笼统、是否连续多轮没有讨论偏好等情况，选择不同的回复策略；未定义的偏好不能任意添加。[S11]、[S12]

其难度来自发现隐含需求，而非让用户总是生气。对已完整给定的 Gaia2 指令，不能机械套用“先说模糊请求、等 Agent 一点点问”的规则，否则测量目标会变化。

### 2.7 CollabLLM：人类与 AI 的协作角色

论文提示将用户设为与 AI 合作者共同完成具体任务的人，输入包括目标描述、完整请求或参考目标、对话历史，用户可提出疑问和反馈。[S13]

这适合说明“合作关系”本身是合理的角色选择。但其部分任务可以给模拟器参考目标，不能据此给 Gaia2 用户隐藏答案、未来事件或业务状态；不同研究的可见信息契约不同。

### 2.8 Dialogue-SWEBench：明确能力边界比空泛身份更关键

用户依据完整 issue 描述与工程师对话，首轮固定且省略部分细节。提示规定用户不能访问终端、仓库或运行新代码，只能使用给定知识答复；工程师负责实现和测试，用户不能假装已经看过对话中没展示的代码。[S14]

这给我们一个具体原则：写明“用户能观察什么、能承诺什么”，避免用户扮演评测管理员，也避免因不知道系统内部结构而被迫编造权限、工具状态或成功结果。论文采用额外修订步骤，但本项目不必因此新增多层验收。

### 2.9 Joi-Bench-Task：受控行为与自然措辞分工

论文的用户模拟器由观察、干预控制、LLM 和 TTS 构成。控制器按任务执行或播报进度选择追问、改变优先级、撤回、追加、暂停/恢复等干预，LLM 负责当时合适的表达与澄清回复。[S15]

这与“给模型一个性格，让它自由发挥到结束”不同。它值得借鉴的是明确合法行为和触发边界；其控制器可读工具事件，是实验调度权限，不意味着用户角色可以口头泄漏后台结果。本次没有核对到该论文完整公开的用户 system prompt，因此不声称其原文采用某个特定人设。

### 2.10 ConvLab / MultiWOZ：agenda 驱动的传统用户模拟

规则用户维护目标和待办议程，接收系统对话行为后更新状态，再产生 inform、request 等用户行为；目标完成或到达轮次上限后结束。[S16]

可借鉴的是目标、已满足项和语言表述可以分开，不必靠一句“像真人一样自然”承担所有控制。规则层比较明确，语言多样性则需要另外实现；它不是直接可替换当前语音 LLM 的现成组件。

### 2.11 UserLM-2.0：专门训练生成用户，而非只提示助手扮演

官方模型由任务意图和历史条件化，使用用户角色生成及专门的结束信号。[S17] 论文同时优化意图忠实度、自然性与结束行为，但明确其训练评估主要来自无工具环境的聊天。[S18]

所以有必要区分“更会演用户”和“知道这个评测环境的规则”。当前直接更换模拟器模型不能自动解决客服角色、应用接入和权限边界问题，不建议将其作为此次问题的首要修复。

### 2.12 Ultravox：用户设定与 Agent 场景配对

维护方说明中，一组 scenario 由 AgentProfile 和 UserProfile 配对，另有工具结果模拟及裁判；这项评测通过文本探索适用于语音产品的模型能力。[S19]

本次未获得完整用户 prompt，不推断其授权、未知信息或结束规则。它与下面 Krutrim 的同名 VoiceAgentBench 是不同项目，不能混用结论。

### 2.13 MINT：反馈代理是另一种用户设定

MINT 使用自然语言反馈帮助 Agent 改进解题，默认反馈不输入参考解，也研究了给参考解的变体。[S20] 它的重点是利用反馈，不是现实来电者身份。

这提醒我们“用户模拟器”没有单一社会角色；但不能把解题反馈代理的特权与纠错能力直接加给 Gaia2 用户，否则会变成向 Agent 提供求解提示。

## 3. 音频评测中的边界对照

| 项目/版本 | 真实机制 | 为什么单列 |
|---|---|---|
| Full-Duplex-Bench | v1/v1.5 用静态录音；v2 使用动态 Examiner；v3 加入真人不流畅语音与工具任务 | 不同版本不能统一描述成一个带人格的自由用户模拟器 [S21]、[S22] |
| Krutrim VoiceAgentBench | 预制合成语音，包含根据多轮信息生成工具调用的任务 | “数据里有多轮”不等于“用户随当前 Agent 动作实时改变下一轮” [S23] |
| APEX-Voice | 固定的预编译用户语音，在专业工作流中测试工具执行、纠正和审批 | 用户行为可由任务状态和脚本控制，无需给所有用户套通用客服性格 [S24]、[S25] |

这几类方法在可重复性与自由互动之间作了不同选择。它们不是所有场景都优于 LLM 用户；但可以证明“语音 Agent 评测必须模拟客服来电者”并不成立。

## 4. 用户模拟器实际需要定义的内容

以下是基于上述材料的设计归纳，不是某一篇论文的原文标准。

| 维度 | 应明确的内容 | 当前 Gaia2 更合适的选择 |
|---|---|---|
| 角色关系 | 谁在向谁委托什么工作 | 用户通过自己的个人助理操作场景应用；没有默认客服机构或主管 |
| 任务目标 | 原始目标、约束、后续合法修订 | 使用已发布请求；dynamic follow-up 保留源请求含义 |
| 事实知识 | 用户已知、未知、可以通过对话获知的信息 | 未提供的人名细节、地址、偏好继续保持未知；不能猜 |
| 应用背景 | 哪些功能已接入、哪些没有 | 可以知道公开的接入类别；不知道隐藏数据库内容和具体求解步骤 |
| 授权 | 某项操作是否在委托范围内、是否需要确认 | 正常确认符合原请求的操作；不自动批准所有未来操作 |
| 信息披露 | 一次给多少，何时澄清 | 按任务选择；完整首轮指令不因通用模板被人为削弱 |
| 交流风格 | 简短、详细、耐心、直接、犹豫等 | 影响说法，不应凭空产生主管、投诉渠道或新任务 |
| 语音行为 | 何时开口、打断、请求重听、拼写 | 根据实际听到的音频及任务需要；独立于客服身份 |
| 等待 | 工作仍在进行时如何回应 | 可等待、确认一次或问进度；不能因几秒沉默默认反复催促 |
| 结束 | 完成、无法继续、放弃、超时 | 对话结束与目标成功分别记录，不能用礼貌告别代替任务成功 |

### 4.1 三种“权限”需要分别表达

1. **应用已经接入**：环境是否给 Agent 工具和连接。这是接入事实，不能由模拟用户一句话创造出来。
2. **用户的业务授权**：是否同意发送这封邮件、取消这场活动、在预算内购买。应以请求范围和正常确认对话为准。
3. **Agent 运行时的确认机制**：后台的操作许可、阻塞和返回流程。需由待测 Agent 正常向用户询问并处理回答。

知道应用已接入，不等于批准所有操作；用户说同意，也不能让没有连接的工具突然可用。用户角色不应负责替 Agent 修复权限机制。

### 4.2 “不知道”与“无偏好”不是同一个状态

不知道联系人姓氏，表示无法提供识别事实；对颜色无偏好，表示可以在允许范围内接受选择。没有指定支付方式也不总等于允许任意扣款。公开项目有各自的简化规则，迁移时应按任务契约决定，不能混成一个通用回答。

### 4.3 不应把所有用户都改成无限配合

现实用户有合理的不耐烦、纠错和拒绝。关于真实人与模拟用户差异的研究发现，模拟者可能过度合作、过早给全信息、对错误反应不足，进而改变 Agent 的成功率。[S26]

适合做的是给“中性委托者、简短直接、健谈、谨慎、容易着急”等风格明确的适用条件。特定投诉或对抗压力测试可以保留，但应单独选择和报告，不应成为每道个人助理题的隐含基准。

## 5. 当前实现与 Gaia2 的不匹配

### 5.1 问题不止开头一句话

当前本地代码至少有这些来源：

- `src/tau2/user/guidelines.py`：voice/plain、voice/tools、text/plain、text/tools 都有客户与客服框架；还有逐步透露、故意模糊、一次只给一项等客服风格规则。
- `src/tau2/user/user_simulator_streaming.py:1872`：每次生成前追加客户身份提醒。
- `data/tau2/multilingual/en/pack.yaml:83`：0760 对应人设含 customer、重复强调、尽早要求主管及强硬回应；声线与这些语用行为绑定。
- `src/tau2/user/user_simulator.py:132`：新阶段用当前请求提醒替换当下目标，但仍沿用正常的通用结束规则。
- `src/tau2/user/dynamic_followup.py`：根据前面对话保留用户感受到的情绪和完成情况。因此早期投诉风格还可能进入后续请求的措辞。
- `src/tau2/runner/build.py:427`：已有其他领域覆盖全局指南和角色提醒的机制；Gaia2 当前没有采用独立的个人助理用户规则。

上述路径均经过本地代码核查。0760 实际送给模型的完整系统提示另存为：[0760 用户系统提示](../data/simulations/qwen-gaia2-random3-scenario-clock-serial-20261122-132202/0760-user-simulator-system-prompt.md)。它包含通话末尾已经发布的三个阶段，不能据此说用户在开场就知道未来请求。

因此，只改“你是客户”为“你是用户”，仍会残留主管、客服身份提醒和偏向投诉的交流规则。另一方面，口音、语言、声线本身没有必要因为修正角色而被移除。

### 5.2 0760 能证明什么，不能证明什么

记录能证明：Agent 首次表示没有账户访问后，用户主要重复任务、质疑和找主管；后半段 ASR 异常进一步造成澄清循环；前台没有任何后台委派和业务操作。

提示词中确实存在对客户身份和找主管行为的鼓励，因而可以解释这种对话风格的来源。但本次没有做控制实验，不能把 Agent 不委派的根因全部归结为用户模拟器，更不能承诺修改角色后奖励一定提高。

## 6. 对当前项目的建议

### 6.1 建议采用的基础设定

以下是本次调研后建议的语义草案，尚未接入代码：

> 你是当前场景中的用户，正在通过语音委托自己的个人助理完成已经发布的任务。你的目标是把需求、必要澄清和授权表达清楚，并理解助手实际报告的结果。
>
> 你知道任务材料中的个人信息、已发布要求和公开的应用接入背景；不知道的事实仍然不知道。你看不到隐藏状态、未来请求、参考答案、工具执行结果或评分规则。不要猜测联系人身份、订单信息或任务是否成功。
>
> 对符合原请求且没有新增后果的确认，可以正常回答；对超预算、改收件人或新增操作，不因助手建议就自动同意。若所需事实未知，明确说明，并可要求助手查询已接入应用。不要凭空要求主管或假设存在客服机构。
>
> 工作进行中可以等待或合理询问进度；已经说明不知道的信息，不要为了推进而捏造。根据实际听到的内容回应；听不清时请求重复。完成、无法继续和放弃对话是不同情况。

“公开接入背景”只能来自实际环境能力声明。例如可以知道通讯录、消息、日历已接入，但不能说某个联系人一定能通过某个工具参数查到，更不能宣称任何写操作都无需用户确认。

### 6.2 不同问题下的合理用户行为示例

这些是设计例子，不是给 Agent 的答案或固定强制回复。

| Agent 表现 | 用户可如何回应 | 边界 |
|---|---|---|
| “我没有日历权限” | 若公开设定确认已接入，可提醒“这是我已接入的日历，请先试着查看；需要我确认哪项操作？” | 不声称后台已经授权，也不教授具体工具调用 |
| 问一个题目未给的姓氏 | “我不知道姓氏，只知道名字。能在通讯录里查一下吗？” | 不捏造姓氏，也不要求无依据选同名联系人 |
| 请求确认原计划中的发送动作 | 在收件人、内容和范围正确时同意 | 同意之后等待结果，不能立即把动作当已完成 |
| 给出工作进展 | 简短确认或等待，必要时稍后问进度 | 进展不是成功，也不是用户要不断重复目标的理由 |
| 返回听不懂的词或数字 | 请重复或换一种说法 | 不把 ASR 错误直接解释为新的任务事实 |
| 确认存在无法解决的阻塞 | 明确接受未完成，或选择合理结束 | 不强迫继续无穷追问，也不因结束而记成功 |

### 6.3 机制上可以保持很轻

后续若实施，可复用现有 domain guidelines override、角色提醒覆盖和当前阶段请求机制；为 Gaia2 组装专用的角色、知识与授权说明。交流风格应与客服语用分开，避免为此改写已发布语言包或冻结题目事实。

不建议因为本次调研再增加一个逐轮评分器、专门的权限裁判、全量重生成或多层验收。先依靠实际提示的组装方式、当前阶段事实边界和正常生成机制表达规则。

只需做少量有针对性的检查：查看实际组装提示是否还残留客服/主管设定；用“无访问能力声明、未知事实、正常确认、工作等待”几个固定上下文检查生成方向。若再跑少量真实题，保留旧结果并注明用户设定版本变化，不直接把新旧成功率当成同一条件下的模型提升。

## 7. 资料使用与证据限制

- 本次对源码/提示词进行了在线阅读，没有运行外部 benchmark，没有下载模型，也没有发起新的评测对话。
- 链接指向调研时的公开页面；部分使用可变化的 main 分支，不能将其当作所有历史版本的共同设定。
- 有源码的项目核对实际模板；仅论文可见的项目只归纳论文机制；只有厂商说明的项目明确降低结论范围。
- GitHub API 目录下载在当前环境失败，本次没有据此声称已经归档或固定外部仓库 SHA；引用以实际成功读取的官方页面为准。
- 没有统计“多少比例 benchmark 用客服角色”，样本也不足以推出全领域频率。这里提供的是设计类型和可复核的具体例子。

## 8. 主要来源

### 原始提示与实现

- **[S1] τ-bench 用户模拟实现**：[官方源码](https://github.com/sierra-research/tau-bench/blob/main/tau_bench/envs/user.py)。核对 `LLMUserSimulationEnv.build_system_prompt`。
- **[S2] τ²/τ-Voice 共用用户指南**：[官方源码](https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/user/guidelines.py)。核对 voice/text、工具/无工具模板。
- **[S3] τ-Voice 论文**：[正式论文页面](https://proceedings.mlr.press/v306/ray26a.html)。核对任务和 full-duplex 用户模拟的研究定位。
- **[S4] EVA-Bench 实际提示**：[simulation.yaml](https://github.com/ServiceNow/eva/blob/main/configs/prompts/simulation.yaml)。核对航空、ITSM、目标、协商、升级和结束规则。
- **[S5] EVA-Bench 配置说明**：[experiment_setup.md](https://github.com/ServiceNow/eva/blob/main/docs/experiment_setup.md)。核对语音用户与 end_call 规则。
- **[S6] ARE UserProxy**：[官方源码](https://github.com/facebookresearch/meta-agents-research-environments/blob/main/are/simulation/agents/user_proxy.py)。核对默认中性角色与 history。
- **[S7] ARE AUI**：[官方源码](https://github.com/facebookresearch/meta-agents-research-environments/blob/main/are/simulation/apps/agent_user_interface.py)。核对代理如何接入消息交互。
- **[S8] ARE / Gaia2 原论文**：[论文 HTML](https://arxiv.org/html/2509.17158v1)。重点核对场景事件结构与 Appendix B.1.2 的 ambiguity 交互边界。
- **[S9] ToolSandbox 用户提示和示例**：[官方源码](https://github.com/apple-aiml-research/ToolSandbox/blob/main/tool_sandbox/scenarios/user_simulator_few_shot_examples.py)。核对 `USER_INSTRUCTION`、设备设置许可与失败结束。
- **[S10] ToolSandbox 项目说明**：[官方 README](https://github.com/apple-aiml-research/ToolSandbox/blob/main/README.md)。核对用户角色和信息/工具缺失测试。
- **[S11] UserBench 用户回复策略**：[utils.py](https://github.com/SalesforceAIResearch/UserBench/blob/main/travelgym/env/utils.py)。核对 preference、elicitation、natural 三种提示。
- **[S12] UserBench 环境实现**：[travel_env.py](https://github.com/SalesforceAIResearch/UserBench/blob/main/travelgym/env/travel_env.py)。核对目标、偏好与交互状态。
- **[S13] CollabLLM**：[论文 HTML，Appendix D.1](https://arxiv.org/html/2502.00640v3)。核对完整用户模拟提示及训练/用户研究边界。
- **[S14] Dialogue-SWEBench**：[论文 HTML，§3.3 与 Appendix A](https://arxiv.org/html/2606.13995)。核对 issue 知识、人设、用户环境限制与 follow-up 提示。
- **[S15] Joi-Bench-Task**：[论文 HTML，§3.2/4.3](https://arxiv.org/html/2611.12118v1)。核对控制器、LLM/TTS 和播报对齐观察机制；完整 system prompt 未核对。
- **[S16] ConvLab-3 MultiWOZ agenda 用户**：[官方源码](https://github.com/ConvLab/ConvLab-3/blob/master/convlab/policy/rule/multiwoz/policy_agenda_multiwoz.py)。核对目标、agenda 更新和结束状态。
- **[S17] UserLM-2.0 官方模型卡**：[Microsoft 模型卡](https://huggingface.co/microsoft/UserLM-2.0)。核对 intent conditioning、用户角色生成和结束信号。
- **[S18] UserLM-2.0 论文**：[论文 HTML](https://arxiv.org/html/2610.31512)。核对任务忠实度/自然性与无工具聊天局限。
- **[S19] Ultravox 的 VoiceAgentBench**：[维护方方法说明](https://www.ultravox.ai/blog/beyond-benchmark-maxxing-measuring-open-source-models-as-real-world-voice-agents)。核对 profile 配对与文本评测定位，不把说明当作完整公开提示。
- **[S20] MINT**：[论文 HTML](https://arxiv.org/html/2309.10691v3)。核对语言反馈代理，以及默认无 GT 与实验变体的区别。

### 音频边界对照与可靠性研究

- **[S21] Full-Duplex-Bench 各版本说明**：[官方 README](https://github.com/DanielLin94144/Full-Duplex-Bench/blob/main/README.md)。
- **[S22] Full-Duplex-Bench v2**：[官方版本说明](https://github.com/DanielLin94144/Full-Duplex-Bench/blob/main/v2/README.md)。
- **[S23] Krutrim VoiceAgentBench**：[官方数据卡](https://huggingface.co/datasets/krutrim-ai-labs/VoiceAgentBench)。注意与 Ultravox 同名项目区分。
- **[S24] APEX-Voice**：[官方源码仓库](https://github.com/apex-voice/code)。核对 frozen synthetic user 描述。
- **[S25] APEX-Voice 数据集**：[作者数据卡](https://huggingface.co/datasets/puneetUMD/APEX-Voice)。核对预渲染音频与专业工作流。
- **[S26] Mind the Sim2Real Gap in User Simulation for Agentic Tasks**：[论文 HTML](https://arxiv.org/html/2603.11245)。参考真实人与模拟用户在合作、披露和错误反应上的差异；本文没有把其统计结果外推到所有领域。

[S1]: https://github.com/sierra-research/tau-bench/blob/main/tau_bench/envs/user.py
[S2]: https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/user/guidelines.py
[S3]: https://proceedings.mlr.press/v306/ray26a.html
[S4]: https://github.com/ServiceNow/eva/blob/main/configs/prompts/simulation.yaml
[S5]: https://github.com/ServiceNow/eva/blob/main/docs/experiment_setup.md
[S6]: https://github.com/facebookresearch/meta-agents-research-environments/blob/main/are/simulation/agents/user_proxy.py
[S7]: https://github.com/facebookresearch/meta-agents-research-environments/blob/main/are/simulation/apps/agent_user_interface.py
[S8]: https://arxiv.org/html/2509.17158v1
[S9]: https://github.com/apple-aiml-research/ToolSandbox/blob/main/tool_sandbox/scenarios/user_simulator_few_shot_examples.py
[S10]: https://github.com/apple-aiml-research/ToolSandbox/blob/main/README.md
[S11]: https://github.com/SalesforceAIResearch/UserBench/blob/main/travelgym/env/utils.py
[S12]: https://github.com/SalesforceAIResearch/UserBench/blob/main/travelgym/env/travel_env.py
[S13]: https://arxiv.org/html/2502.00640v3
[S14]: https://arxiv.org/html/2606.13995
[S15]: https://arxiv.org/html/2611.12118v1
[S16]: https://github.com/ConvLab/ConvLab-3/blob/master/convlab/policy/rule/multiwoz/policy_agenda_multiwoz.py
[S17]: https://huggingface.co/microsoft/UserLM-2.0
[S18]: https://arxiv.org/html/2610.31512
[S19]: https://www.ultravox.ai/blog/beyond-benchmark-maxxing-measuring-open-source-models-as-real-world-voice-agents
[S20]: https://arxiv.org/html/2309.10691v3
[S21]: https://github.com/DanielLin94144/Full-Duplex-Bench/blob/main/README.md
[S22]: https://github.com/DanielLin94144/Full-Duplex-Bench/blob/main/v2/README.md
[S23]: https://huggingface.co/datasets/krutrim-ai-labs/VoiceAgentBench
[S24]: https://github.com/apex-voice/code
[S25]: https://huggingface.co/datasets/puneetUMD/APEX-Voice
[S26]: https://arxiv.org/html/2603.11245

# Speech-to-Speech 接入与单题验收

已接通整套 Qwen Audio Agent 的级联语音前台：MiMo ASR → 文本 LLM → MiMo TTS。
真实验收只运行 retail 英文任务 **1**，单次、240 秒，没有重跑。
原生 Qwen Code 后台执行了 **3 次业务 MCP 调用，3 次返回，0 次工具错误**。
该题因达到时间上限结束，得分 **0**，业务目标尚未完成。

## 实际运行配置

| 部分 | 配置 |
| --- | --- |
| 语音前台 | 原生 `speech-to-speech` provider；Hugging Face Speech-to-Speech 1.1.0 |
| Agent ASR | `mimo-v2.5-asr` |
| 前台文本 LLM | `gpt-6.1-sol`；Responses API 流式工具调用；reasoning `low` |
| Agent TTS | `mimo-v2.5-tts`；音色 `Dean` |
| 后台 | 原生 Qwen Code 0.26.0；MiniMax-M3；reasoning `low`；原生权限模式 |
| 评测侧声音与转写 | MiMo TTS 和独立的已播放音频 MiMo ASR observer |
| 镜像 | `docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.4.0` |

镜像 ID：`sha256:3e9851710a827faa3b43381566fc6f44dd278e1fcfa7a217fddffb4845087efe`。
Qwen 源码版本：`d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9`，工作区保持干净。
新增代码均在 τ 仓库；原配置文件没有修改。前台读取 `LLM_*` 和 `MIMO_API_KEY`，
后台读取原有 `GAIA2_BACKEND_*`，无需 DashScope 密钥或 Realtime 模型。

## 单题结果与证据

运行目录：[qwen-s2s-smoke-20261024-task1](../../data/simulations/qwen-s2s-smoke-20261024-task1/)。
Simulation ID：`209625ec-3922-4883-8d25-fd3452894f35`。
音频预算 240 秒；框架记录的实际运行耗时约 276.91 秒。
终止原因 `max_steps`，框架对这次未完成的运行记录 0 分。

实际业务调用分别为：

1. `find_user_id_by_name_zip`
2. `get_user_details`
3. `get_order_details`

调用与返回记录均存在于完整 trajectory 的 `ticks` 中，3 个返回的 `error` 均为 `false`。
轨迹保存了实际前台模型、ASR/TTS、后台模型、镜像 ID 和文件隔离证明。
Observer 完成 9 段实际已播放语音的转写，无待处理片段；释放延迟平均约 1246 ms，最大约 1538 ms。
这项延迟仅描述评测侧 observer，不代表 Agent ASR 或整轮对话延迟。

- [验收摘要](../../data/simulations/qwen-s2s-smoke-20261024-task1/acceptance_summary.json)
- [结果清单](../../data/simulations/qwen-s2s-smoke-20261024-task1/results.json)
- [完整轨迹](../../data/simulations/qwen-s2s-smoke-20261024-task1/simulations/209625ec-3922-4883-8d25-fd3452894f35.json)
- [双方录音](../../data/simulations/qwen-s2s-smoke-20261024-task1/artifacts/task_1/sim_209625ec-3922-4883-8d25-fd3452894f35/audio/both.wav)
- [Agent 输出录音](../../data/simulations/qwen-s2s-smoke-20261024-task1/artifacts/task_1/sim_209625ec-3922-4883-8d25-fd3452894f35/audio/taps/agent_output.wav)

录音声道包含静音，其总长度为 240 秒。结果中的 Agent 费用为未知；现有价格表缺少相关服务，
日志中的价格映射提示不影响这次调用和返回，但打印费用不能用作账单。

## 耗时复核

240 秒是这次联通试跑显式指定的对话预算。当前仓库普通语音运行默认 1200 秒，
发布版 voice 预设对非 elicitation 域默认 2400 秒。因此，本次上限适合快速确认链路，
不能据此认定完整换货任务理应在四分钟内完成。终止记录为 `max_steps`，没有触发
本次另设的 390 秒实际运行超时。

按录音/评测 tick 时间轴近似对齐，时间分配如下；它与包含启动、调度开销的实际秒数不同：

| 对话时间 | 状态 |
| --- | --- |
| 0–约 84 秒 | 多轮说明需求、重复确认及身份核验；随后前台提交后台任务 |
| 约 84–167 秒 | 后台发现工具，完成查用户、读用户信息、读订单三次业务查询 |
| 约 167–240 秒 | 下一步商品查询触发原生权限确认；后台处于 `auth_required`，约 73 秒未继续业务操作 |

权限请求产生后，Agent 在 198.6–204.6 秒实际播放第一段授权询问，评测侧约第 203.4 秒
才收到包含“May I proceed?”的转写；第二段授权询问实际播放于 219.4–228.0 秒。
模拟用户随后在 229.4–238.4 秒完整发送了含“Yes, please proceed”的新一轮语音，
并附带商品规格条件。截止 240 秒，Gateway/τ 日志中没有该轮用户语音的最终转写通知，
也未观察到 `respond_permission` 调用或后续商品查询。
应区分用户已表达同意与 Agent 已收到、理解并提交批准；前者已经发生，后者没有完成。
日志末尾的权限 resolved 记录发生在运行清理阶段，不能作为正常批准、继续执行的证据。
因此，这次有明确的权限交互停滞；单纯增加时间不能保证任务会完成。

进一步核对，请求已以 `task.permission.requested` 到达 Gateway/前端系统，没有丢失。
这里等待的是 Qwen Code 的工具执行授权，针对接下来的商品信息查询；评测中需要由
τ 模拟用户通过语音作出决定。模拟用户只接收实际播放的语音及其转写，不读取结构化权限事件，
也没有浏览器/TUI 上可直接点击的批准按钮。

原生播报协调器在用户说话、当前回复未结束或有排队音频时将前端视为忙碌，推迟专门的授权询问。
本次请求产生时模拟用户已经在说话，随后普通对话回复与继续插话让播报窗口一直受阻。
约第 204.8、228.4 秒，两次 `origin=permission` 的专门回复终于启动，但都被新的用户语音取消；
两次均没有音频块实际送给评测端。普通回复中另有授权询问，用户在最后一轮表达了附带条件的同意，
但截止前 Gateway 没有收到该轮最终转写通知，也未记录提交批准，后台保持等待。
应区分“前端系统收到请求”和“用户及时听清并完成批准”；此次阻塞发生在后面的语音交互过程。

按真实播放区间逐段复核，可排除这几处均属于“同一句话没说完，Agent 就抢话”的解释：

| 用户实际播放 | Agent 实际播放或事件 | 对应情况 |
| --- | --- | --- |
| 165.4–172.6 秒，补充价格要求 | 约 167 秒产生权限事件，没有立即播出授权提示 | 此刻用户确实还在讲这一轮补充 |
| 186.8–191.0 秒，补充规格要求 | 198.6–204.6 秒播放授权询问 | 用户上一轮已经结束，间隔约 7.6 秒才回复 |
| 204.6–211.6 秒，新一轮追问键盘 | 约 204.8 秒启动的额外权限提示被取消；219.4–228.0 秒播放第二段询问 | 额外提示碰上用户新一轮发言；第二段询问在用户结束后约 7.8 秒才开始 |
| 229.4–238.4 秒，新一轮“Yes, please proceed…”并附带条件 | 228.0 秒上一段 Agent 语音已结束；228.4 秒启动的额外提示被新发言取消 | 用户已回答授权，但截止前 Gateway 没有收到该轮最终转写通知，也没有提交批准 |

前十轮用户语音从实际发送结束到 Gateway 对外收到最终转写通知，按对话时间轴测得约 4.6–13.6 秒，
平均约 8.2 秒；最后一轮结束时只剩 1.6 秒预算。该数值不是内部 ASR 完成时间。
Speech-to-Speech 1.1.0 会先将识别结果送入内部聊天和生成队列，再把最终转写以及 speech_stopped
通知保留到该轮确认后对外发布。因此，通知延迟可能还包含模型准备回复及提交该轮的等待。
Qwen Gateway 又使用这些对外的说话状态来调度授权播报，两者对同一轮状态的时序存在衔接问题。
现有记录既不能把这段时间全部归因于 MiMo ASR，也不能证明最后一轮在内部完全没有被识别。
逐轮记录见 [turn_timing_analysis.json](../../data/simulations/qwen-s2s-smoke-20261024-task1/turn_timing_analysis.json)。

三次业务查询分别在第 103.2、121.4、151.6 秒被处理，均在对应的同一 tick 内返回，
没有工具执行错误。查询之间的实际间隔约 20 秒、32 秒，包含后台模型决策、工具发现等工作，
不能将其解释为业务 MCP 接口本身耗时几十秒。

前台有六轮可配对的语音结束到开始播放回复记录，间隔约 1.5–8.6 秒。
评测侧模拟用户的 11 次文本生成平均约 4.6 秒、最高约 10.1 秒；对应 TTS 平均约 4.0 秒、
最高约 9.0 秒。这些活动存在并行，不能直接相加作为总耗时，也不能全部算作被测 Agent 的耗时。

现有日志没有记录断连、HTTP 请求失败、请求超时、限流或重连。
费用映射/缺少费用的日志属于统计提示；每 40 秒一次的不活跃告警则存在统计口径问题：
适配层的事件是字典，通用 `has_provider_activity` 用属性读取事件类型，因而没有识别这些活动。
该告警只打印日志，不触发暂停或终止；同一时期仍有语音和工具事件，不能据此判断断网。
目前没有逐请求网络、服务端排队和推理的独立计时，不能排除网络延迟的影响或精确拆分其占比。

另观察到部分 ASR 转写存在不完整片段及额外标记。其对重复沟通的影响尚未单独验证，
当前不能认定它是主要耗时原因。本次复核没有新增评测或修改被测系统。

## 验证

- 相关 Python 测试：28 项通过。
- 镜像系统检查：13 项通过，包括真实 Gateway、ACP、Qwen Code、原生级联服务、MCP、权限确认和语音输出；仅远端模型使用模拟服务。
- 平台文件隔离检查通过，包括子进程继承、隐藏目录、跨目录移动和硬链接；实际 UID/GID 保持不变。
- 标准 provider suite：132 项因各自实时 API 开关未启用而跳过；这与上述实际单题验收分别记录。
- Ruff 检查、格式检查与 `git diff --check` 通过；τ 主 `uv.lock` 未改变。
- 已扫描本次生成的文本、JSON 和日志，未发现配置中的密钥值；本次容器已清理。

## 运行入口

配置：[config.speech-to-speech.example.json](config.speech-to-speech.example.json)。
构建脚本会校验静态资源、锁定 CPU 依赖并完成镜像系统检查；运行时自动管理每题服务。

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --config integrations/qwen_audio_agent/config.speech-to-speech.example.json \
  --task-ids 1 --max-seconds 240 --save-to qwen-s2s-task1-new
```

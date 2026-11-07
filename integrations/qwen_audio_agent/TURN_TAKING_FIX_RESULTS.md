# 异步模拟用户发言调度修复与单题验收

状态：修复、离线验证和唯一一次真实验收均已完成。调度验收通过；该题业务得分为 **0**，以转人工结束。

## 修复内容

修复位于 τ 的 `VoiceStreamingUserSimulator` 公共异步路径，版本为 `v1.2`。
Qwen Audio Agent 源码、镜像、模型、提示词、原生权限和业务评分规则沿用上次配置。

旧逻辑把“异步追问准备完成”直接视为“立即开口”，会跳过 Agent 此刻是否已经回复的判断。
新版在结果交付前重新检查实际收到的输入：

- 普通追问、普通工具结果回复在准备期间收到新的 Agent 语音或转写后作废。Agent 此后即使已经说完，也不会恢复播放旧句子。
- 有原策略授权的主动打断和简短应答，在同一段 Agent 发言中仍可播出；该段结束或被新段替代后重新决策。
- 语音段边界沿用现有 `integration_ticks`。新语音及新转写更新输入版本，静音和重复转写不更新；同一转写事件文本发生修订时仍算新输入。
- 生效检查同时覆盖语音、`STOP` 和用户工具调用。实际已执行的工具结果保留在历史中，作废语音不会重复执行工具。
- 运行中的 HTTP 请求只能标记作废，保留 Future 直至异步回收；未启动的请求取消。保持单个发言生成和单个监听判断，不堆积替代请求，音频 tick 继续运行。
- 发言计数、拼读事件和实际播放队列只在候选被接受后更新。监听结果、随机状态和决策元数据也按有效性处理。

生成来源显式记录为普通回复、主动打断、简短应答或工具结果回复，不依赖解析日志文字。
未知价格继续保持未知；已作废生成的 LLM 费用和监听判断费用纳入额外开销，避免漏算或重复计算。
TTS 费用仍未知，其耗时可从生成事件读取。本轮不把费用统计当作完整服务账单。

## 记录与兼容

- `TurnTakingAction.generation_origin` 为新增可选字段。
- `Info.voice_user_simulator_version` 记录本轮 `v1.2`；旧结果读取后仍为未记录，不会自动标成新版。
- `SimulationRun.info.user_generation` 保存事件来源、输入版本、生成／作废／开口时刻、用量及额外开销。
- 现有续跑条件检查会拒绝新旧调度版本混用。本次验收使用独立新目录，历史结果保留。
- 只有实际接收的 Agent 音频与 observer 转写用于调度判断，内部 Gateway 完整答复不进入用户听到的内容。

## 离线验证

相关回归测试 **76 项通过**，另有 **11 项 τ-Entity 运行兼容性测试通过**。
所有这些测试均使用可控 Future 或模拟外部 LLM/TTS，不发起真实模型请求。

覆盖了准备期间收到新回复、旧生成在 Agent 说完后才完成、延迟及修订转写、静音、正常插话与简短应答、
过期监听判断、旧 `STOP` 和用户工具调用、工具结果保留、发言与拼读状态、请求费用、异常回收及续跑版本隔离。
同时保留了音频 tick 不被 LLM/TTS 请求阻塞的回归检查。

现有 τ-Entity 拼读检查改为通过真实的候选交付路径验证事件，以符合“仅接受的发言才产生拼读事件”的规则。
其正常调用提示、发音配置及普通语音栈默认值检查仍通过。

## 唯一一次真实验收

运行目录：[qwen-s2s-turntaking-v12-task2-20261024-214811](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/)。
retail 英文任务 **2**，单次、进程并发 1；运行 seed **300**，对应 trial seed **626729**。
保留 `--max-seconds 0`：`info.max_steps=null`，没有整次运行超时，不自动重测。

语音前台继续使用 MiMo ASR → `gpt-6.1-sol` → MiMo TTS，后台为 Qwen Code 0.26.0／MiniMax-M3。
镜像继续使用现有 `0.4.0`，本次没有构建新镜像。

验收重点是普通追问是否依据最新已接收内容决定开口，以及正常插话是否有策略授权。
业务得分按原规则如实记录，不要求靠重复运行获得 1 分。若复现同一种调度缺陷，则停止本次验收并记录失败。

### 实际验收结果

| 项目 | 结果 |
| --- | --- |
| 实际运行耗时 | 约 17 分 38 秒 |
| 正常结束方式 | `agent_stop`，执行 `transfer_to_human_agents` |
| 普通发言实际开口 | 47 次 |
| 收到新回复后作废的普通追问 | 6 次 |
| 通话结束时回收的未播普通生成 | 1 次 |
| 过期普通追问开口 | 0 次 |
| 业务工具调用 / 返回 / 错误 | 22 / 22 / 6；错误均为身份查询“用户不存在” |
| 数据库检查 | 0；三件商品的退货没有提交 |
| “10 种可售 T 恤”自然语言断言 | 1，通过 |
| 最终业务奖励 | `DB 0 × NL_ASSERTION 1 = 0` |

没有新增时长截断，没有人工结束这次通话，没有自动重跑。
这次实际没有调用 `return_delivered_order_items`，最终转人工，因此不能将业务目标记为完成。
独立重载语言绑定并严格回放全部工具调用后，数据库不匹配的结果与原始评分一致。

调度检查除了核对生成事件，也独立扫描了保存的完整 tick 轨迹：对全部 47 次普通开口，
检查生成开始后至开口前模拟用户已经收到的 Agent 语音及文本，没有发现应作废却被播出的普通追问。
同一 tick 内尚未传到用户的新 Agent 输出不算用户已知输入。
本轮还有一次主动打断候选，因其对应的 Agent 发言已结束而作废；同段内正常主动打断和简短应答的可用性由离线回归验证。

验收进程正常退出，本轮容器已移除。运行期间相关实现文件的校验值保持一致。
相关源文件及测试通过 Ruff 和格式检查，`git diff --check` 通过；Qwen 工作区干净，主 `uv.lock` 未变。
已检查本轮生成的文本、JSON 与日志，未发现私有配置中的密钥值。

- [结果清单](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/results.json)
- [实现校验值与测试记录](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/implementation_provenance.json)
- [最终验收摘要](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/acceptance_summary.json)
- [生成事件检查](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/scheduler_verification.json)
- [独立的实际输入检查](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/recorded_input_verification.json)
- [密钥值检查结果](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/secret_scan.json)
- [离线回归日志](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/validation/offline_tests.log)
- [τ-Entity 兼容性日志](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/validation/elicitation_compatibility.log)
- [完整轨迹](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/simulations/bf0eeb0d-da4c-4bca-b1a1-794afd15dad9.json)
- [双方原始录音](../../data/simulations/qwen-s2s-turntaking-v12-task2-20261024-214811/artifacts/task_2/sim_bf0eeb0d-da4c-4bca-b1a1-794afd15dad9/audio/both.wav)

修复前的三题结果及实际播放中断证据见 [UNLIMITED_THREE_RESULTS.md](UNLIMITED_THREE_RESULTS.md)。

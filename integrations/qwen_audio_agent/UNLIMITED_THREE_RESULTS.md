# 取消时长截断后的三题联通验收

状态：三题均已结束并完成评分，得分均为 1。所有评测进程和本轮容器均已退出。
本轮使用完整 Qwen Audio Agent 的 Speech-to-Speech 配置，实际测试 retail 英文任务 0、1、2，各一次。
任务 2 在 2026-10-24 18:59:39 自行调用转人工工具结束；收到停止指令后检查时它已结束，没有额外发送终止信号。

## 时长限制

`smoke` 默认使用 `--max-seconds 0`。它同时关闭对话 tick 上限与整次评测的实际运行超时；
结果中 `audio_native_config.max_steps_seconds=0`，`info.max_steps=null`。
普通 τ 运行的默认时长保持原设置；显式指定 `--max-steps-seconds 0` 可使用不限时模式。
单个网络请求、启动失败和工具错误的终止机制仍保留。1200 秒的输出队列容量不限制通话总时长。

## 实际结果

耗时列是框架记录的实际运行时间，包括模型和服务等待；录音时长按评测 tick 计量，与实际运行时间不同。

| 任务 | 实际运行耗时 | 正式得分 | 结束原因 | 业务调用 / 返回 / 错误 |
| --- | --- | --- | --- | --- |
| 0：键盘和恒温器换货 | 33 分 15 秒 | 1 | `user_stop` | 31 / 31 / 0 |
| 1：仅恒温器换货，保留原键盘 | 29 分 23 秒 | 1 | `user_stop` | 23 / 23 / 5 |
| 2：查询 T 恤选项并退三件商品 | 56 分 01 秒 | 1 | `agent_stop`，转人工 | 22 / 22 / 3 |

三题的得分均有最终业务数据库匹配支撑。独立加载保存的完整轨迹、重载对应语言数据并执行严格数据库回放，
复核结果与原始评分一致；评分没有依赖前台口头声称“已完成”。任务 1 的五次错误均为身份查询返回
`Error: User not found`，随后账户识别及换货成功；它们不改变该题按最终数据库状态计算的奖励。

任务 0、1 没有自然语言断言，`NL_ASSERTION=1` 是空检查的中性值，未调用断言裁判。
任务 2 的“Agent 应告诉用户有 10 种 T 恤选项”断言已由实际裁判检查并通过。
这三题的奖励依据为 `DB × NL_ASSERTION`；任务 2 的一项只读参考动作未匹配，不属于奖励依据。
任务 2 的原始业务目标完成后，额外的标签日期问题最终转交人工处理。
这些得分验证了评分计算与业务结果，不能据此判定整个语音交互调度正确。

评分侧的自然语言断言裁判改为读取已配置的 `GAIA2_JUDGE_*`，本轮为
`openai/responses/MiniMax-M3`、reasoning `low`；题目断言、评判提示和奖励乘积规则保持原样。
实际使用的断言裁判记录在评分元数据中。正式试跑之外已用非题目问候语验证裁判连接及 JSON 解析。

## 被测系统与运行组织

被测系统为原生语音前台、Gateway、任务运行时和 Qwen Code 后台组成的完整 Agent：
MiMo ASR `mimo-v2.5-asr` → `LLM_*` 配置的 `gpt-6.1-sol` → MiMo TTS `mimo-v2.5-tts`。
后台使用 Qwen Code 0.26.0、MiniMax-M3 和原生权限流程。评测侧另用 MiMo 播放用户语音并转写实际播放的 Agent 音频。

镜像为 `docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.4.0`，
镜像 ID 为 `sha256:3e9851710a827faa3b43381566fc6f44dd278e1fcfa7a217fddffb4845087efe`。
Qwen 源码版本为 `d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9`。
本轮没有修改 Qwen 源码、原生权限或用户私有配置文件，也没有重建镜像。

每题使用独立容器、工作区及会话。后两题通过独立进程启动，每个进程并发数为 1；三题配置与随机种子一致。
原始批次在任务 0 的完整结果保存后停止排队，未截断任务 0 的对话；随后进入的任务 1 初始化仅留下启动日志，
未建立语音会话或发起业务调用。独立目录中的任务 1 是唯一有实际对话和评分的任务 1 样本。

实际运行保留了长时间重复确认、识别误差和用户追加问题等现象。业务提交成功后仍可能进行很多轮对话，
因此本轮已经确认的评分成功不等于交互效率良好。

## 第三题长时间重复追问的播放复核

不能把 Gateway 中完整的答复文本当作模拟用户已经收到的完整回复。最近几轮的模拟用户输入上下文只有
`The email address is yu.` 等开头片段，缺少邮箱后半段和标签日期说明。
已用 Gateway 清空事件与评测 tick 的实际音频字节计数核对，确认语音本身被中断：

| Agent 开始输出音频 | 用户开始新一轮语音 | Agent 实际播放时长 | 尚未播放即丢弃的队列音频 |
| --- | --- | --- | --- |
| 18:47:33.872 | 18:47:36.758 | 约 3.4 秒 | 约 1.56 秒 |
| 18:49:03.459 | 18:49:03.909 | 约 1.0 秒 | 约 5.40 秒 |
| 18:49:24.443 | 18:49:26.914 | 约 3.0 秒 | 约 2.12 秒 |
| 18:49:49.239 | 18:49:50.045 | 约 1.4 秒 | 约 4.68 秒 |

以上为 2026-10-24 本地时间，来自运行中的日志；队列音频不等于完整答复的全部剩余语音，后续合成也可能因取消而停止。
这四轮的下一句用户发言均在 Agent 开始出声前约 4.2～5.4 秒就以“两方静音阈值已满足”为由启动准备。
准备期间 Agent 开始回复，但用户生成完成后仍播出此前准备的追问，触发 Gateway 清空播放。
因此，本次循环涉及评测侧异步用户发言调度与 Agent 回复时序的衔接，不能全部归因于 Agent，
也不能认定用户已经听到完整答案后仍在追问。

证据：[播放与用户生成时间对齐记录](../../data/simulations/qwen-s2s-unlimited-20261024-task2/partial_reply_delivery_analysis.json)。
另截取了 [18:49:49 附近的 16 秒双方原始录音](../../data/simulations/qwen-s2s-unlimited-20261024-task2/diagnostics/turn_taking_184949.wav)，
仅裁剪原始双声道 PCM，未重新合成或重采样；[裁剪位置](../../data/simulations/qwen-s2s-unlimited-20261024-task2/diagnostics/turn_taking_184949.json)与评测 tick 对齐。
本轮尚未修改这部分调度逻辑；完整轨迹和录音均已保存。

## 调度原因及修复建议

问题位于 `src/tau2/user/user_simulator_streaming.py`：

1. `_start_generation()` 使用当时的对话快照，异步准备用户追问及 TTS。
2. 准备期间，新的 Agent 语音和转写仍会继续进入评测。
3. `_next_turn_taking_action()` 先检查已有生成任务是否完成；完成即返回 `generate_message`，
   早于后面的“Agent 是否正在讲话”和主动打断策略判断。
4. `_perform_turn_taking_action()` 直接播放旧快照生成的追问。新输入虽然留给下一轮使用，
   但不会修正当前这句即将播出的旧追问。

这几轮属于先因静音准备普通追问、后与新到的 Agent 回复冲突；没有经过基于新回复的主动打断判断。
已用不调用模型或 TTS 的最小例子复现：给定“Agent 正在讲话、用户生成任务已完成”，
当前代码仍选择 `generate_message`，甚至没有读取 Agent 是否正在讲话。
证据：[最小复现结果](../../data/simulations/qwen-s2s-unlimited-20261024-task2/turn_taking_reproduction.json)。

建议在评测侧为普通追问增加“开口前重新判断”的环节：

- 准备期间 Agent 开始回复时，继续接收其实际播放的语音和转写，重新检查开口时机。
- 旧追问已经被新回复回答时，将它作废，听完后按最新内容重新生成；仅延后播放旧句子仍会重复追问。
- 为普通追问、经过判断的主动打断和简短应答保留不同的生成来源，主动打断继续遵循原有策略。
- 保持音频实时传输和真实计时，保留丢弃生成的实际调用成本与诊断记录，不把未播出的文本算作已听到的对话。

回归验证应覆盖：生成期间 Agent 开始说话、Agent 已说完但旧生成才完成、未收到新回复、
有意主动打断、简短应答和异步任务异常。此处是修复建议，当前报告不宣称调度问题已经修复。

## 证据

- [三题复核摘要](../../data/simulations/qwen-s2s-unlimited-20261024-three/three_task_verification.json)
- [合并后的三题结果](../../data/simulations/qwen-s2s-unlimited-20261024-three/results.json)
- [任务 1 原始结果](../../data/simulations/qwen-s2s-unlimited-20261024-task1/results.json)
- [任务 2 原始结果](../../data/simulations/qwen-s2s-unlimited-20261024-task2/results.json)
- [聚合来源与原始轨迹校验值](../../data/simulations/qwen-s2s-unlimited-20261024-three/aggregation_provenance.json)
- [停止请求检查结果](../../data/simulations/qwen-s2s-unlimited-20261024-task2/stop_request_status.json)

完整轨迹位于各结果目录的 `simulations/`；双方录音和原生桥接事件位于
`artifacts/task_<id>/sim_<id>/audio/`。历史 240 秒试跑保留在
[SPEECH_TO_SPEECH_RESULTS.md](SPEECH_TO_SPEECH_RESULTS.md)，与本轮结果分别存储。

## 代码验证与复现

最终相关回归检查 12 项通过；此前不限时、评分及断点续跑检查 14 项通过，二者有重叠。
相关修改通过 Ruff 检查与格式检查，`git diff --check` 通过；τ 主 `uv.lock` 和 Qwen 工作区没有变更。

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True integrations/qwen_audio_agent/run_hpc.sh smoke \
  --config integrations/qwen_audio_agent/config.speech-to-speech.example.json \
  --task-ids 0 1 2 --max-seconds 0 --save-to qwen-s2s-unlimited-new
```

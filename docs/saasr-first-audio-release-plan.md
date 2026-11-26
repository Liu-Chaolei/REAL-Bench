# SAASR 首音频释放与暂停式生成计划

## 1. 背景

当前 SAASR 用户语音生成需要等待完整音频返回后，才能进入分块播放。用户
LLM、SAASR TTS 和场景混音之间的等待关系如下：

```text
用户 LLM 完成
    ↓
SAASR 完整合成整段音频
    ↓
得到完整 PCM 音频
    ↓
切分并开始播放
```

这会把整段 TTS 的准备时间放在用户首次开口之前。为了降低首包延迟，需要
支持流式 SAASR 音频：首个可播放 chunk 到达后恢复模拟世界，后续 chunk 在
后台继续生成和播放。

本计划只描述运行时机制。它不增加 evaluator、reward、judge、benchmark
metrics 或任务完成规则，也不把音频生成状态写入评测逻辑。

## 2. 目标语义

新增的行为是“首音频释放”：

```text
用户 LLM/TTS 开始
    ↓
模拟世界暂停，等待首个可播放音频
    ↓
首个音频 chunk 到达
    ↓
恢复模拟世界和 Agent tick
    ↓
后续音频继续在后台生成并进入播放队列
```

它等待首个音频 chunk，而不是等待整段音频完成。首 chunk 到达前，以下时钟
保持暂停：

- `FullDuplexOrchestrator` 的模拟 tick；
- `ScenarioRuntime` 的场景时间；
- SAASR 场景播放位置；
- 后台场景事件调度；
- 用户音频的播放时间线。

首 chunk 到达后，世界恢复运行。后续音频生成速度低于播放速度时，播放器
输出场景音频或静音，并等待下一段；队列永远不能无限增长。

这是一种新的运行时释放策略。它不等同于完整的 realtime generation，也不等同
于等待整段语音完成的严格同步模式。

## 3. 状态模型

用户模拟器为每个候选回复维护一个 `PendingUserGeneration`，至少包含：

```text
generation_id
input_revision
speech_epoch
text
audio_queue
first_chunk_ready
completed
cancelled
error
```

状态转换如下：

```text
created
  → generating_text
  → generating_audio
  → first_chunk_ready
  → playing
  → completed
```

任何阶段都可以进入：

```text
cancelled
failed
```

候选被取消时必须同时完成以下动作：

- 取消 SAASR worker 中尚未生成的音频；
- 清空未播放的音频队列；
- 不把未播放文本写入正式对话历史；
- 不增加用户已播放 utterance 计数；
- 记录内部运行事件，便于排查生成和播放竞态。

## 4. SAASR worker 接口

当前完整合成接口返回一整段音频：

```python
audio, metadata = session.synthesize(text)
```

需要增加一个流式接口，放在 SAASR 语音层：

```python
stream = session.synthesize_stream(text)
```

流对象需要提供：

- `first_chunk_ready`：首个 PCM16 16 kHz chunk 可用；
- `audio_queue`：后续 chunk 队列；
- `completed`：合成结束；
- `error`：worker 失败；
- `cancel()`：取消未完成的合成。

音频 chunk 必须保持固定的 PCM16 16 kHz 单声道格式。chunk 大小应和现有
SAASR tick 的输入约定一致，或在 SAASR session 边界统一切成固定长度。

队列需要设置最大容量，避免 TTS 生成速度长期高于播放速度时无限堆积。队列
满时应暂停或背压 worker，而不是丢弃正式主声音频。

## 5. 用户模拟器流程

用户模拟器决定执行 `generate_message` 后：

1. 创建 `PendingUserGeneration`；
2. 保存当前输入 revision 和 speech epoch；
3. 启动用户文本生成和 SAASR 流式合成；
4. 暂停模拟世界；
5. 阻塞当前调用，直到首个音频 chunk、失败或取消；
6. 首 chunk 到达后恢复模拟世界；
7. 将首 chunk 放入输出队列并返回；
8. 后续 tick 从输出队列取音频；
9. 后台线程继续向队列写入后续 chunk；
10. 队列结束后把候选标记为 `completed`。

等待首 chunk 期间不能让 orchestrator 继续推进普通 tick，否则“世界暂停”
会被破坏。首 chunk 到达后，后续播放和场景混音才能恢复。

## 6. SAASR 场景混音

SAASR 场景的主声、背景人物声和环境声由 `SaasrSession.mix_tick()` 统一混音。
首 chunk gate 期间不调用新的场景 tick，也不增加 SAASR 播放位置。

恢复后，每个 tick 依次完成：

1. 从用户音频队列取当前主声 chunk；
2. 读取当前场景背景和环境音频；
3. 调用 `mix_tick()`；
4. 封装为用户 PCM16 16 kHz 消息；
5. 发送给 Agent；
6. 记录实际发送的 SAASR 输出。

如果主声队列暂时为空但候选还没有完成，场景仍可以输出背景和环境音频。
如果整个候选已经取消，主声停止，场景可以继续直到当前 turn 的播放边界。

## 7. 时钟暂停与恢复

`ScenarioRuntime` 当前有独立的墙钟线程。只暂停 orchestrator 调用不足以实现
世界暂停，因为该线程仍可能推进事件和释放用户请求。

需要增加运行时暂停协议：

```python
runtime.pause()
runtime.resume()
runtime.is_paused
```

暂停期间：

- `ScenarioRuntime.time()` 保持不变；
- scheduler 线程不执行 due event；
- 不释放新的 scripted user input；
- 不执行依赖场景时间的环境操作；
- 不推进后台任务触发器的模拟时间。

恢复时继续使用同一个场景时间，不把墙钟等待时长补进模拟时间。

暂停状态必须支持嵌套保护或引用计数，避免一个生成任务恢复了另一个仍在等待
的生成任务。

## 8. Qwen adapter 音频边界

Qwen adapter 的外部输入格式固定为 SAASR 输出：

```text
PCM16 16 kHz mono
```

Qwen provider 的实际输入采样率由连接握手协商。adapter 在 provider 边界做
PCM16 重采样，不引入 μ-law 8 kHz 电话转换。

Qwen provider 的播放输出仍可以是 PCM16 24 kHz。adapter 负责把 provider
输出转换成统一的 playback 格式，保证：

- 初始 Agent 音频；
- provider 返回的音频；
- playback queue；
- `TickResult`；
- observer 看到的已播放音频；

在同一播放轨道上使用一致的格式。

如果某个 legacy observer 只接受 8 kHz PCM，可以只在 observer 边界做转换。该
转换不能重新进入 Agent transport，也不能改变 SAASR 用户输入格式。

## 9. 取消和打断

以下事件会使当前候选失效：

- Agent 新的语音输入改变了用户上下文；
- Agent 结束当前 speech episode；
- Agent 明确停止或转人工；
- 场景切换释放了更高优先级的 scripted input；
- simulation 结束。

取消流程必须先关闭播放授权，再取消 SAASR worker：

```text
close generation admission
    ↓
标记候选 cancelled
    ↓
停止继续向播放队列写入
    ↓
清空未播放 chunk
    ↓
取消 SAASR 合成
    ↓
回收 future 和临时文件
```

已经送给 Agent 的音频不能撤回，也不能重新排序。它只按实际发送和实际播放
时间进入现有轨迹。

## 10. 错误处理

首 chunk 之前失败：

- 恢复模拟世界；
- 抛出用户模拟器或基础设施错误；
- 不产生空的正式用户回复；
- 保留失败原因和生成 ID。

首 chunk 之后失败：

- 保留已播放的主声；
- 停止后续主声；
- 继续输出必要的场景背景或静音；
- 将当前候选标记为失败；
- 不自动切换到 standard TTS。

队列出现格式错误、chunk 长度错误或时间线倒退时，应立即结束当前候选并
报告基础设施错误，避免静默产生错误音频。

## 11. 轻量机制测试

测试使用 fake SAASR worker，不调用真实模型、GPU、Agent 或评测系统。

需要覆盖：

1. fake worker 延迟后返回首 chunk，确认首 chunk 前世界时间不变；
2. 首 chunk 到达后确认模拟 tick 恢复；
3. 后续 chunk 逐步进入播放队列且顺序不变；
4. worker 慢于播放时不会死锁或无限增长；
5. 首 chunk 前取消不会产生正式用户语音；
6. 首 chunk 后打断会清空剩余候选；
7. 新 Agent revision 会使旧候选失效；
8. ScenarioRuntime 暂停期间不执行 due event；
9. PCM16 16 kHz 输入、Qwen provider 输入和 PCM16 24 kHz playback 的格式边界正确；
10. observer 的兼容性转换不改变 Agent transport。

不运行完整 benchmark、不重新评分历史结果、不增加 judge、不修改 reward，
也不要求三道真实题作为这个机制的通过条件。

## 12. 实施顺序

1. 增加 SAASR fake streaming worker 和最小流式接口；
2. 增加 PCM16 streaming resampler 和格式边界测试；
3. 给 `ScenarioRuntime` 增加 pause/resume；
4. 在用户模拟器中加入 `PendingUserGeneration`；
5. 实现首 chunk gate 和后续队列播放；
6. 实现候选取消、打断和 worker 回收；
7. 接入 Qwen adapter 的 PCM16 16 kHz 输入和 playback 格式；
8. 更新 Qwen smoke 配置，使 SAASR 和 realtime 成为固定运行条件；
9. 运行 focused 机制测试和格式化、lint 检查；
10. 仅在 worker、SAASR 配置和 GPU 环境准备好后，另行做少量人工 smoke。

## 13. 交付边界

本计划交付的是运行时音频和时钟机制：

- 首音频优先播放；
- 世界暂停和恢复；
- 连续 SAASR 场景混音；
- 后台候选取消；
- PCM 格式边界统一。

评测系统只接收最终已有的用户消息、Agent 消息和工具轨迹。生成中间状态、
首 chunk 延迟和取消次数只用于运行时日志或 SAASR 音频 artifact，不进入评分
输入和 benchmark schema。

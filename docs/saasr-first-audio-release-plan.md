# SAASR 首音频释放方案

## 1. 背景和目标

当前 SAASR 用户回复的流程是：用户模拟器生成文本，TTS 生成完整音频，完整音频返回后才进入播放队列。这样首个用户音频要等待整段回复处理完成。

目标是把流程改成：

```text
用户 LLM 完成
    ↓
开始 TTS / SAASR 音频流
    ↓
首个可播放音频 chunk 到达
    ↓
立即恢复模拟主循环并输出
    ↓
后续 chunk 在后台继续生成、排队和播放
```

需要等待的是“首个可播放 chunk”，不是整段回复的全部音频。首个 chunk 输出后，用户模拟器不能再次同步等待剩余音频。

本方案只修改运行时机制，不把状态写入 evaluator、reward、judge、benchmark metrics 或任务完成规则。监控只保留必要的本地日志和单元级状态检查。

## 2. 需要先区分的两个实现级别

当前 Qwen TTS HTTP 适配器返回完整 WAV。若服务端没有流式响应，就无法从网络响应中真正拿到第一个 PCM 包。因此分两步实现：

### 第一阶段：句子级首音频释放

- 将用户回复按句子切分；
- 先请求第一句 TTS；
- 第一整句音频完成后立即输出；
- 后续句子在后台请求并进入队列。

这能消除“等待整段回复全部 TTS 完成”的问题，且不要求修改 Qwen TTS 服务协议。

### 第二阶段：PCM chunk 级首音频释放

如果 Qwen TTS 提供流式接口，再把 adapter 改成增量解析音频响应：

- 第一个可播放 PCM chunk 到达时触发 `first_audio_ready`；
- 不等待第一句完整 WAV；
- 后续 PCM chunk 持续进入队列。

第二阶段只替换 TTS 生产端，用户模拟器、SAASR 混音和 orchestrator 使用同一套 stream 接口。

## 3. 音频流状态模型

每个用户回复维护一个独立的 `SaasrStream`，至少包含：

```text
generation_id
turn_id
input_revision
audio_queue
first_audio_ready
completed
cancelled
error
```

状态转换：

```text
created
  → generating
  → first_audio_ready
  → playing
  → completed
```

任意阶段都可以进入：

```text
cancelled
failed
```

接口建议保持简单：

```python
stream = session.synthesize_stream(texts)
first = stream.wait_first(timeout=...)
item, done = stream.poll()
stream.cancel()
stream.close()
```

`wait_first()`只负责首音频 gate。首 chunk 返回后，调用方只能使用非阻塞的 `poll()`，不能再次调用一个等待整段音频完成的接口。

## 4. SAASR 和 Qwen TTS 适配器

主要修改：

- [src/tau2/voice/saasr.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/voice/saasr.py)
- [src/tau2/voice/qwen_tts.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/voice/qwen_tts.py)

实现要求：

1. `SaasrStream` 使用后台 producer 生成句子或 PCM chunk。
2. 第一个可播放 chunk 入队时设置 `first_audio_ready`。
3. 后续音频按顺序入有界队列。
4. 队列满时背压 producer，不能无限占用内存。
5. producer 结束时设置 `completed`。
6. producer 出错或取消时唤醒 `wait_first()` 和 `poll()`，不能留下永久等待。
7. 生成结果保持 SAASR 统一格式：PCM16、单声道、16 kHz。
8. Qwen TTS 回调继续使用 `qwen-audio-3.1-tts-flash`，不切回本地 foreground 模型。

Qwen TTS 返回的 16 kHz PCM 进入 SAASR 混音。Qwen Agent transport 的 16 kHz 输入和 24 kHz 播放输出仍由各自 adapter 边界处理，不重新引入 8 kHz 电话格式。

## 5. 用户模拟器流程

主要修改：[src/tau2/user/user_simulator_streaming.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/user/user_simulator_streaming.py)

用户模拟器生成回复时：

1. 完成用户 LLM 生成。
2. 创建当前 turn 的 `SaasrStream`。
3. 启动后台音频 producer。
4. 只等待第一个可播放 chunk、错误或取消。
5. 第一个 chunk 到达后立即构造 `UserMessage` 并返回。
6. 设置 `is_final_chunk=False`，保留 stream 引用。
7. 后续 tick 调用非阻塞 `poll()`，把已完成的 chunk 放入输出队列。
8. 队列暂时为空且 producer 未完成时，返回 `wait`，不能同步等待整个 TTS。
9. 最后一个 chunk 播放完成后标记当前 stream 为 `completed`。

首音频输出后，后续 TTS 生成不再占用当前 `get_next_chunk()` 调用的等待时间。用户 LLM 仍然需要先完成，因为当前模式的目标只是取消“等待全部音频处理完成”，不改变用户文本生成模式。

## 6. orchestrator 和场景时钟

主要修改：[src/tau2/orchestrator/full_duplex_orchestrator.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/orchestrator/full_duplex_orchestrator.py)

当前暂停逻辑需要缩小到首音频 gate：

- 在等待首音频期间暂停 `ScenarioRuntime`；
- 首 chunk 到达后立即恢复 `ScenarioRuntime`；
- 后续 chunk 的 `poll()` 不触发新的长时间暂停；
- 后续后台 TTS 继续运行，但不阻塞 orchestrator tick；
- Agent 独立接收和播放链路继续按现有机制运行。

暂停期间保持场景时间和 due event 不变。恢复时继续原来的模拟时间，不把等待首音频的墙钟时长补进场景时间。

暂停和恢复必须成对执行，并使用引用计数或明确的 owner，避免一个生成任务恢复另一个仍在等待的任务。

## 7. SAASR 混音和播放队列

首音频 gate 结束后，每个 tick 按以下顺序处理：

1. 从用户音频队列取当前可用 chunk；
2. 读取对应的 SAASR 背景和环境音频；
3. 调用 `mix_tick()`；
4. 输出 PCM16 16 kHz 用户消息；
5. 发送给 Agent；
6. 记录实际发送的播放时间线。

如果 producer 暂时没有新 chunk：

- producer 未结束时，输出当前允许的背景或静音，并在后续 tick 继续轮询；
- producer 已结束时，关闭当前用户音频 stream；
- stream 取消时清空尚未播放的主声 chunk。

队列必须保持音频顺序，不能因为后台任务完成顺序不同而重排用户声音频。

## 8. 打断、取消和过期结果

以下情况取消当前 stream：

- Agent 打断当前用户回复；
- 新的 Agent 输入使当前用户回复失效；
- 用户停止或场景结束；
- simulation 完成；
- 当前 turn 的 input revision 发生变化。

取消顺序：

```text
关闭当前 stream 的输出授权
    ↓
标记 cancelled
    ↓
清空未播放 chunk
    ↓
取消 TTS / SAASR producer
    ↓
回收 future 和临时资源
```

后台 producer 即使晚返回，也必须检查 `generation_id`、`turn_id` 和 `input_revision`。旧 stream 的结果不能写入新一轮输出队列。

首 chunk 之前失败时，恢复场景时钟并按现有用户基础设施错误处理。首 chunk 之后失败时，保留已发送的音频，停止后续主声，避免自动切换到 standard TTS 造成格式和时序混乱。

## 9. 轻量验收边界

不写评测系统验收逻辑，只做以下检查：

1. 用假的慢速 producer 验证：第一 chunk 到达后，`get_next_chunk()` 立即返回，第二个 chunk 延迟到达也能继续输出。
2. 验证队列为空时不会再次同步等待整段 TTS。
3. 验证取消后不会输出旧 turn 的音频。
4. 跑一题 SAASR + Qwen TTS smoke，检查首音频时间和 stream 完成时间的先后关系。

建议只保留少量内部日志：

```text
generation_id
first_audio_ready_at
stream_completed_at
queued_chunks
cancelled
error
```

这些日志用于机制排查，不写入 reward、trajectory 评分字段或 benchmark 结果。

## 10. 实施顺序

1. 先整理 `SaasrStream` 的状态、队列、取消和错误唤醒逻辑。
2. 接入句子级后台 producer，保持现有 Qwen TTS 完整 WAV 接口不变。
3. 修改用户模拟器，只等待首 chunk，后续改为非阻塞轮询。
4. 缩小 orchestrator 的暂停范围，确保首 chunk 返回后立即恢复场景时间。
5. 加入 turn ID、input revision 和取消保护。
6. 运行轻量 stream 单元检查和一题 smoke。
7. 如果 Qwen TTS 后续提供流式接口，再将句子级 producer 替换为 PCM chunk producer。

完成后的核心行为是：首个可播放音频一旦准备好，主循环马上继续；剩余音频由后台按顺序完成，不再把整段音频处理时间叠加到一次用户响应的同步等待中。

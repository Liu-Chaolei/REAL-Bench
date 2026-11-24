# SAASR 与交互插入链路拆分计划

## 目标

在 `user_audio_renderer=saasr` 模式下，把当前混在一起的两类职责拆开：

```text
主用户语音链路
用户文本 / backchannel
        ↓
SAASR 主声合成与连续场景混音
        ↓
直接输出给 Agent

交互插入链路
交互空档判断
        ↓
非定向语音、咳嗽、喷嚏等插入
        ↓
作为额外短时轨道叠加到 SAASR tick
```

SAASR 模式下，Tau 原来的主用户语音合成和声学效果链路全部退出。交互插入仍然保留，但它作为独立的行为链路运行，不再借用 Tau 主声链路的处理流程。

`standard` 模式继续保留给非 SAASR 运行使用。本计划只改变 SAASR 模式的构建和输出路径。

## 明确的边界

SAASR 模式保留：

- 用户初始请求、后续回复和 backchannel 的 SAASR 合成；
- SAASR 场景中的主声、背景人物和环境噪声；
- SAASR 自己的混音、统一增益和场景播放位置；
- 交互空档中的非定向语音、咳嗽、喷嚏、吸鼻子等插入；
- 插入音频的等待、分块播放、取消和事件记录。

SAASR 模式移除：

- Tau 原来的主用户文本到 TTS 路径；
- Tau 的持续背景噪声；
- Tau 的突发噪声；
- Tau 的主声 dynamic muffling；
- Tau 的 frame drop；
- Tau 对主声文本做 vocal tic 文本插入；
- Tau 的 `BackgroundNoiseGenerator`、普通 `EffectScheduler` 主声处理和 `process_streaming_chunk()` 主声效果处理；
- Tau 的电话格式转换。

最后一项需要特别明确：SAASR 混音后的输出直接作为用户输出发送给 Agent，不再经过 `StreamingTelephonyConverter`、μ-law 转换、8 kHz 降采样或其他 Tau 传输处理。SAASR 输出的 PCM 格式和采样率由 SAASR 链路直接提供，用户模拟器不再改写它。

如果某个 Agent provider 在协议层强制要求其他音频格式，那属于该 provider 的输入适配责任，不重新放回用户渲染链路，也不通过 Tau 的电话转换器处理。

## 当前代码中的耦合点

当前 SAASR 分支在 [`VoiceStreamingUserSimulator._apply_chunk_effects()`](../src/tau2/user/user_simulator_streaming.py) 中直接跳到 `_mix_saasr_chunk()`。这会同时绕过：

- Tau 的背景和突发噪声；
- Tau 的 frame drop；
- 交互空档插入；
- 插入音频的 pending 状态和事件记录。

当前 [`VoiceMixin.synthesize_voice()`](../src/tau2/agent/base/voice.py) 还负责标准模式的主声 TTS、muffling、文本 vocal tic 插入和部分声学处理。SAASR 主声不应再调用这个入口。

当前 [`OutOfTurnSpeechGenerator`](../src/tau2/voice/synthesis/audio_effects/speech_generator.py) 已经能预生成非定向语音和 vocal tic，并通过 [`PendingEffectState`](../src/tau2/voice/synthesis/audio_effects/processor.py) 分块播放。这部分机制可以复用，但需要从普通音效处理器中抽出来，避免重新带入背景噪声、突发噪声和 frame drop。

## 配置和对象拆分

### 主声配置

SAASR 主声继续使用 `SaasrSession`。它负责：

- 场景准备；
- foreground 合成；
- SAASR 背景和环境噪声；
- 主声和场景的混音；
- 统一增益；
- SAASR 输出格式。

### 插入配置

在 [`runner/build.py`](../src/tau2/runner/build.py) 把主声设置切换为 SAASR 之前，保存一份独立的插入配置：

```text
main_synthesis_config
    → SAASR 主用户文本

interaction_insert_config
    → 非定向语音、咳嗽、喷嚏、吸鼻子等短时插入
```

插入配置只需要包含：

- 插入内容列表；
- 插入频率；
- 插入音频生成 provider 和 provider config，或预录音频目录；
- 插入内容的声音处理选项。

主声切换为 `provider="saasr"`、`provider_config=None` 后，不能再从主声配置中寻找插入 TTS。

建议给 `VoiceStreamingUserSimulator` 显式增加 `interaction_insert_config` 或 `interaction_insert_engine` 参数。这样主声使用 `saasr_session`，插入使用 `interaction_insert_engine`，两个职责在构造函数层面就分开。

## 新的交互插入引擎

建议新增一个轻量的 `InteractionInsertEngine`，可以放在 `src/tau2/voice/interaction_inserts.py`，也可以放在用户模拟器相关模块中。它只负责：

1. 根据当前是否处于交互空档决定是否允许触发；
2. 按配置和随机种子选择插入内容；
3. 预生成或加载插入音频；
4. 维护一个跨 tick 的 pending 插入；
5. 在主用户开始说话时取消尚未完成的空档插入；
6. 生成插入事件和音频轨道元数据。

它不负责：

- 连续背景噪声；
- 突发噪声；
- 网络丢帧；
- 主用户文本 TTS；
- 电话格式转换。

原 `EffectScheduler` 可以继续供 `standard` 模式使用。SAASR 模式不再创建它，也不通过设置 `enable_background_noise=False` 等方式伪装关闭原链路。

## SAASR tick 的新处理顺序

SAASR 模式下每个 tick 的逻辑应为：

1. 判断当前 chunk 是否包含主用户有效语音；
2. 如果当前是交互空档，让 `InteractionInsertEngine` 判断是否触发插入；
3. 如果存在 pending 插入，取出当前 tick 的插入音频；
4. 如果主用户开始说话，取消尚未播放完的插入；
5. 将主用户 chunk 和插入音频交给 `SaasrSession.mix_tick()`；
6. 由 SAASR 完成统一增益和场景混音；
7. 直接把 SAASR 输出封装成用户消息；
8. 直接发送给 Agent，不做 Tau 电话格式转换。

建议把 `SaasrSession.mix_tick()` 扩展为接收额外的插入轨道：

```python
saasr_session.mix_tick(
    main_chunk=main_chunk,
    interaction_insert=insert_audio,
    count=self.chunk_size,
)
```

插入轨道要在 SAASR 内部参与统一增益计算，不能在 `mix_tick()` 返回 PCM 后再临时相加，否则保存的混音轨道、发送给 Agent 的数据和峰值控制可能不一致。

SAASR 的内部证据轨道可以从：

```text
main
background
noise
mixed
```

扩展为：

```text
main
background
noise
interaction_insert
mixed
```

`sent_to_agent.wav` 应直接记录 SAASR 输出本身，不再经过 μ-law 或其他 Tau 电话编码。

## 主声 vocal tic 的处理

标准链路可能在主声 TTS 前把 vocal tic 文本插入用户句子。SAASR 模式下不再执行这一步：

```text
原始用户文本保持不变
SAASR 合成主声
咳嗽/喷嚏作为独立插入音频
```

这样可以避免把 `[cough]`、`[sneeze]` 等标签交给 SAASR，也不会污染正式用户文本、转写文本或场景语义。

交互空档的 vocal tic 继续由 `InteractionInsertEngine` 处理。它属于插入事件，不属于主声 TTS。

## 插入音频的来源

插入音频可以继续使用原来的 TTS provider，但必须通过独立的插入配置调用，不得调用主声的 `VoiceMixin.synthesize_voice()`。

建议的优先级是：

```text
非定向语音
    → 原有 TTS 生成短句

咳嗽、喷嚏、吸鼻子
    → 优先预录音频素材
    → 没有素材时使用兼容的 TTS 音频标签
```

当前这些标签主要依赖 ElevenLabs v3。Qwen 或其他 TTS 不一定会把标签解释成真实声音，因此不能把标签直接放进 SAASR 主声文本。

非定向语音原先的特殊闷音处理可以留在插入引擎内部，因为它描述的是该插入事件的声音表现，不是主用户主声的 Tau 音效链路。

## 状态和取消行为

交互插入需要独立维护：

- `pending_insert`：跨 tick 播放的当前插入；
- `insert_event_id`：插入事件标识；
- `generated`、`started`、`completed`、`discarded` 状态；
- 当前插入的播放偏移。

行为规则：

- Agent 说话或双方等待时，允许判断是否触发插入；
- 主用户正式语音开始时，取消尚未完成的插入；
- 已经送出的插入不能回放或重排；
- 通话结束时清理 pending 插入和插入音频资源；
- 插入音频生成失败只记录插入失败，不回退到 Tau 主声 TTS，也不回退到另一条主声链路。

## 需要清理的 SAASR 分支代码

SAASR 分支需要移除以下调用关系：

- `get_init_state()` 中的 `BackgroundNoiseGenerator` 初始化；
- SAASR 分支对 `EffectScheduler` 的初始化和调用；
- SAASR 分支对 `process_streaming_chunk()` 的主声处理；
- SAASR 分支的 `_add_telephony_compression()`；
- SAASR 分支的 `StreamingTelephonyConverter`；
- 主声文本对 `insert_speech_text()` 的依赖；
- 主声对 Tau dynamic muffling、burst noise、frame drop 的依赖。

需要保留或新增：

- `SaasrSession` 的主声和场景渲染；
- `InteractionInsertEngine`；
- 插入音频的 pending 和取消逻辑；
- SAASR 内部的 `interaction_insert` 轨道；
- 直接输出 SAASR 音频的消息封装。

## 数据记录

不修改 reward、judge、benchmark manifest 或任何评测指标。只在 SAASR 音频证据中记录插入过程，便于复现和排查：

```json
{
  "event": "interaction_insert",
  "kind": "non_directed",
  "text": "Hold on a second.",
  "start_sample": 123456,
  "duration_samples": 9600,
  "played": true
}
```

音频证据保留：

- `played_main.wav`；
- `played_background.wav`；
- `played_noise.wav`；
- `played_interaction_insert.wav`；
- `played_mixed.wav`；
- `sent_to_agent.wav`。

这里的 `sent_to_agent.wav` 是未经 Tau 电话转换的 SAASR 输出。

## 轻量验证

不做完整 benchmark、全量评测或真实 GPU 验收。只做机制级测试，使用 fake SAASR session 和 fake 插入音频：

1. SAASR 主用户文本不会调用 Tau 主声 TTS；
2. SAASR 模式不会创建 Tau 背景噪声、突发噪声、frame-drop 或电话转换状态；
3. SAASR 输出直接进入用户消息，音频格式和采样率不被 Tau 改写；
4. 交互空档可以触发非定向语音或 vocal tic；
5. pending 插入可以跨多个 tick 播放；
6. 主用户重新开始说话时，未完成插入会被取消；
7. 插入音频同时出现在 `interaction_insert` 和 `mixed` 轨道；
8. `standard` 模式的既有路径仍然可用。

验证只覆盖调用边界、状态转换和音频轨道合成，不把插入事件纳入评分，也不改变评测系统行为。

## 实施顺序

1. 增加独立的插入配置和 `InteractionInsertEngine`；
2. 在 runner 构建阶段保存插入 TTS 配置，再将主声配置切换到 SAASR；
3. 让 SAASR 初始化只创建 `SaasrSession` 和插入引擎；
4. 扩展 `SaasrSession.mix_tick()` 接收插入轨道；
5. 删除 SAASR 分支中的 Tau 主声效果和电话格式转换；
6. 接入 pending 插入、取消和事件记录；
7. 增加少量 fake 机制测试；
8. 运行现有 SAASR targeted tests 和新增机制测试，不运行完整评测。


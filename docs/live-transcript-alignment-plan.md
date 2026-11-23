# Live Transcript 对齐放宽计划

## 目标

语音评估中，某个已播放音频片段没有得到 ASR 文字时，保留这段证据，但不要因此取消整通电话的 `COMMUNICATE` 或 `NL_ASSERTIONS` 评分。

目标行为：

```text
有音频但没有 ASR 文字
    -> 保留 Live segment 记录
    -> 标记该片段没有文字证据
    -> 跳过该片段
    -> 使用其他有 ASR 文字的片段继续评分
```

普通 Gaia2 语音评分不使用 Agent backend 的 `generated_text` 代替 ASR。`generated_text` 继续只服务于 persistent task 已有的 `reply_evidence_mode="generated_text"` 路径。

## 现状

Live transcript 流程把实际播放音频和 observer ASR 文字按播放时间分组，生成 `live-segment:<id>`。当某个音频区间没有对应 ASR fragment 时，会设置：

```json
{
  "association_status": "unmatched_audio",
  "audio_alignment_error": "Recorded Live speech has no transcript",
  "fragments": []
}
```

`require_live_alignment()` 当前对所有 `audio_alignment_error` 抛出异常。full-duplex 的 `COMMUNICATE` 和 `NL_ASSERTIONS` 在转换评分文本前调用它，因此一个缺少 transcript 的片段会阻止整通电话评分。

`0214` 暴露了这个问题：最后约 200ms 的播放音频有能量，但 MiMo ASR 返回空文字，导致整通 `COMMUNICATE` 评估失败。这个片段不应代表整通电话都无法评分。

## 处理规则

### 可跳过的情况

只放宽这一类错误：

```text
Recorded Live speech has no transcript
```

对应的 Live segment 保留在轨迹和证据中，但不生成用于文本评分的 Agent utterance。

### 继续拦截的情况

以下情况仍然表示时间或证据结构损坏，继续抛出错误：

- ASR 文字没有任何可对应的播放音频；
- 音频时间区间非法或超出 tick 范围；
- ASR 文字时间顺序倒退；
- 音频截断无法归属到任何 response；
- 其他无法解释的音频/文字错位。

## 实现步骤

### 1. 保留 Live segment 证据

保留 [live.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/utils/transcript/live.py) 现有的分组和错误记录逻辑。

`live_utterance_groups()` 仍然生成缺失 transcript 的 segment，并保留 `association_status`、`audio_alignment_error`、播放时间和 tick 范围。这样问题仍可回放和诊断。

### 2. 放宽 `require_live_alignment()`

在 [live.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/utils/transcript/live.py) 中调整 `require_live_alignment()`：

- 过滤 `Recorded Live speech has no transcript`；
- 对其他 alignment error 保持现有异常行为；
- 不新增 judge、不调用额外模型、不改变 ASR 请求。

可以保留一个内部的 skipped 计数或 metadata 字段，用于说明有多少片段被跳过，但不引入新的 headline metric。

### 3. 让 transcript view 跳过空片段

在 [views.py](/aistor/hpc_stor03/sjtu_home/chaolei.liu/Audio-Agent/tau2-bench/src/tau2/utils/transcript/views.py) 的 Agent transcript view 中：

- 有 ASR fragments 的 Live segment照常进入 `agent_messages()` / `speech_messages()`；
- `fragments=[]` 且错误为 `Recorded Live speech has no transcript` 的 segment 不生成评分文本；
- 其他 segment 的顺序、segment ID 和文本保持不变。

这样普通评分只会看到可验证的 ASR 内容，不会把缺失片段当成空白句子，也不会回退到 Agent 生成文字。

### 4. 保持两个评分器的输入边界

`COMMUNICATE` 和 `NL_ASSERTIONS` 继续读取 Live transcript view：

```text
实际播放音频 -> observer ASR -> 可用 Live transcript -> judge
```

它们不读取 Qwen Gateway 的 `generated_text`。

Persistent task 保持现有行为：当 `reply_evidence_mode="generated_text"` 时，仍可使用 backend 生成文字作为功能性状态回复证据；这个路径与普通 Gaia2 语音评分分开。

ENV 和 ACTION 评分不需要修改，它们继续使用工具调用、工具结果和环境状态。

## 最小测试

只增加验证机制的单元测试，不做大规模 live 验收：

1. 一个最后短片段有音频但没有 ASR 时，`require_live_alignment()` 不抛异常。
2. transcript view 会跳过该片段，并保留其他正常片段。
3. 普通 `COMMUNICATE` 不会从 `generated_text` 构造 fallback 文本。
4. 普通 `NL_ASSERTIONS` 可以使用剩余 ASR 片段继续构造对话。
5. ASR 无音频、时间越界、顺序倒退等真正的 alignment error 仍然抛异常。
6. persistent task 的 `generated_text` 测试保持通过。

现有 Naturalness / Speech Fidelity 不纳入这次改动范围。

## 预期结果

`0214` 这类轨迹会从：

```text
一个 200ms 音频尾部没有 ASR
    -> 整通 COMMUNICATE 失败
```

变为：

```text
正常 ASR 片段继续评分
200ms 无 ASR 尾部被跳过
整通 COMMUNICATE / NL 继续计算
```

这次改动只放宽缺失 transcript 的证据处理，不改变 Agent 行为、用户模拟器、ASR 请求、工具执行或任务 reward 定义。

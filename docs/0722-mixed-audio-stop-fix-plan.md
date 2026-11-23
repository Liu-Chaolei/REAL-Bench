# 0722 多阶段语音消息类型错误修复计划

## 1. 问题现象

Gaia2 任务 `0722_fvkmflhob7blr325ifgvys8onydq7nqi` 在第一阶段结束、准备进入第二阶段时中断，错误为：

```text
ValueError: All chunks must be either audio or non-audio. Found mixed types: [False, True]
```

这不是 Live transcript 对齐错误，也不是 ASR 失败。异常发生在用户模拟器为下一次 LLM 调用整理自己的 Tick 历史时，模拟尚未进入评分阶段，因此结果是 `infrastructure_error`。

## 2. 根因链路

0722 是三阶段任务：

1. 第一阶段监控四分钟内新增的日历事件。
2. 第二阶段发送监控窗口审计邮件。
3. 第三阶段发送改期提议邮件。

第一阶段用户模型生成类似下面的结束消息：

```text
Alright, talk later. Thanks again — bye. ###STOP###
```

`###STOP###` 在中间阶段表示当前阶段完成。当前代码的顺序是：

1. 用户模拟器的 `_generate_full_duplex_voice_message()` 识别到 STOP，直接返回文本消息，不经过 TTS，因此该消息的 `is_audio=False`。
2. `StreamingMixin.get_next_chunk()` 在阶段控制器处理之前，把这条原始 STOP 消息写入用户模拟器的私有 `state.ticks`。
3. `ScenarioStages` 将 STOP 识别为内部阶段结束，并排队下一阶段用户请求；全局 Tick 中的消息会被替换为空消息，但用户模拟器私有历史中的原始 STOP 仍然存在。
4. 下一阶段请求经过 TTS 后，以 `is_audio=True` 的用户消息写入同一份私有历史。
5. 用户模拟器下一次调用 `state.get_linearized_messages()` 时，containment-aware linearizer 将相邻的同角色消息视为同一连续语音段，调用 `consolidate_messages()` 合并它们。
6. `can_merge_messages()` 目前只检查消息类别和角色，没有检查 `is_audio`；最终 `merge_message_chunks()` 收到 `[False, True]` 并抛出异常。

异常位置主要在：

- `src/tau2/user/user_simulator_streaming.py` 的历史线性化调用。
- `src/tau2/utils/transcript/linearization.py` 的连续消息合并。
- `src/tau2/data_model/message.py` 的 `is_audio` 一致性检查。

## 3. 修复目标

- 中间阶段的 STOP 必须成为阶段边界，不能和下一阶段语音合并。
- 用户模拟器在生成后续 LLM 上下文时不能因为文本消息和音频消息相邻而崩溃。
- 保留 `merge_message_chunks()` 对真正非法混合音频 chunk 的保护。
- 不改变 Live alignment、ASR、communicate 评分或最终阶段 STOP 的语义。
- 不让内部 STOP 出现在后续普通用户 LLM 上下文中，避免把控制信号当成用户业务内容。

## 4. 实现方案

### 4.1 用户模拟器丢弃私有历史中的控制 STOP

在 `VoiceStreamingUserSimulator` 的 `get_next_chunk()` 返回 `super()` 结果后，检查返回的用户消息是否为 STOP/TRANSFER/OUT-OF-SCOPE 控制消息。

如果是控制消息，并且 `state.ticks` 的最后一个 `self_chunk` 正是这条控制消息，则把最后一个 Tick 的 `self_chunk` 清空。这样：

- 全局 orchestrator 仍然能看到原始 STOP，并按现有 `ScenarioStages` 逻辑推进阶段或结束模拟。
- 用户模拟器私有 LLM 历史不会保存只用于控制流程的 STOP 消息。
- 下一阶段的音频消息不会和上一阶段的控制消息相邻合并。
- 最终阶段的 STOP 也可以从私有历史中移除，因为检测到最终 STOP 后不会再生成后续用户回复；全局轨迹仍保留最终停止结果。

建议抽出一个小的私有辅助方法，例如 `_discard_last_control_message_from_history(state, message)`，只负责：

1. 检查消息是否为控制信号。
2. 检查最后一个 Tick 是否保存了对应的 `self_chunk`。
3. 使用 `model_copy(update={"self_chunk": None})` 替换最后一个 Tick，避免直接修改 Pydantic 对象内部字段。

不要在 orchestrator 中复制用户历史结构，也不要改变 `ScenarioStages` 的推进逻辑。

### 4.2 合并条件增加音频模式检查

在 `src/tau2/utils/transcript/linearization.py` 的 `can_merge_messages()` 中增加：只有当两个 participant message 的 `is_audio` 值相同时才允许合并。

这样即使未来仍有其他路径把文本消息和音频消息写到同一段历史中，linearizer 也会保留两个消息边界，而不会调用 `merge_message_chunks()` 触发 `[False, True]` 异常。

这个检查是防御性保护，不能替代 4.1。内部 STOP 仍应从用户私有历史中移除，因为它本身是控制信号，不应作为后续业务对话内容发送给用户 LLM。

不要放宽 `merge_message_chunks()` 中已有的 `is_audio` 一致性校验。该校验可以继续拦截调用方直接传入的非法混合 chunk。

### 4.3 保持动态 follow-up 校验问题独立

0722 日志中还出现过动态 follow-up checker 拒绝第一版文案的情况，原因是文案加入了未经历史证明的 “still unresolved”。这属于动态 follow-up 文案校验问题，不是本次基础设施崩溃的根因。

本次修复不修改动态 follow-up 的语义校验；如果重跑后仍出现 checker 拒绝，应按动态 follow-up 的重试/回退逻辑单独分析。

## 5. 测试计划

### 5.1 线性化单元测试

在现有 streaming 或 transcript 测试中增加一个轻量测试：

1. 构造一个 `is_audio=False`、内容包含 `###STOP###` 的 `UserMessage`。
2. 构造紧随其后的 `is_audio=True` `UserMessage`。
3. 放入相邻 `ParticipantTick`。
4. 调用 `linearize_ticks(..., CONTAINMENT_AWARE)`。
5. 断言不抛异常，并且两条消息没有被错误合并。

同时保留一条同为音频 chunk 的合并测试，确认正常音频分片仍然合并。

### 5.2 用户模拟器历史测试

增加一个不调用真实 LLM/TTS 的测试，验证控制消息返回后：

- 全局返回值仍然包含 STOP，调用方仍能识别停止。
- 用户模拟器状态最后一个 Tick 的 `self_chunk` 已被清除。
- 后续追加音频消息后调用 `get_linearized_messages()` 不会失败。

### 5.3 现有相关测试

优先运行：

```bash
uv run pytest tests/test_streaming/test_scenario_voice.py \
  tests/test_streaming/test_voice_streaming_user_simulator.py \
  tests/test_streaming/test_chunking.py
```

然后运行与 transcript linearization 相关的已有测试。如果环境缺少可选语音依赖，只运行不需要真实 Provider 的测试，不启动 live API。

### 5.4 轻量真实验证

代码测试通过后，只重跑 0722 一题，使用原来的同步用户生成、DeepSeek backend、动态 follow-up 配置。

验收重点：

- 不再出现 `All chunks must be either audio or non-audio`。
- 任务至少能从 stage 0 进入 stage 1。
- 如果后续因 Agent 工具、超时或评分内容失败，应记录为新的独立问题。
- 不要求重新跑全部五题，也不做 Naturalness / Speech Fidelity 的重验收。

## 6. 实施顺序

1. 先在 `linearization.py` 增加 `is_audio` 相同条件，加入最小复现测试。
2. 在 `VoiceStreamingUserSimulator` 中加入控制 STOP 私有历史清理逻辑。
3. 增加用户模拟器状态测试，确认全局 STOP 语义没有被清理逻辑影响。
4. 运行目标测试和 Ruff 检查。
5. 单独重跑 0722，检查阶段推进和错误类型。
6. 根据 0722 重跑结果决定是否需要处理其他独立问题；不把动态 follow-up 文案校验、Agent 工具行为和 Live transcript 问题混入本次修复。

## 7. 风险与处理

- **风险：删除 STOP 后动态 follow-up 上下文少了告别语句。** 这是可接受的，因为 STOP 是流程控制信号，阶段状态由 `ScenarioStages` 保存；动态 follow-up 只需要业务对话和工具结果。必要时可以在私有 system context 中增加非业务的阶段完成标记，但第一版不引入新的提示词协议。
- **风险：仅修改合并条件会产生相邻同角色消息。** 用户 LLM 历史目前没有强制角色交替校验；同角色消息仍可被翻转并发送。控制 STOP 清理逻辑会使正常路径不再产生这种边界，合并条件只是兜底。
- **风险：误删普通用户语句。** 清理逻辑必须只针对 `is_stop()` 识别出的控制消息，并且只清理最后一个 Tick 中对应的 `self_chunk`。
- **风险：影响最终停止评估。** 清理只作用于用户模拟器私有状态；orchestrator 的返回 chunk、全局 Tick、termination reason 和最终轨迹保持原有流程。

## 8. 完成标准

实现完成后，满足以下条件即可结束本次修复：

1. 0722 不再因 `[False, True]` 音频模式混合而产生 infrastructure error。
2. 中间阶段 STOP 可以触发下一阶段请求。
3. 最终阶段 STOP 仍然能结束模拟。
4. 现有音频 chunk 合并行为和 Live transcript 处理测试通过。
5. 0722 单题重跑至少能进入第二阶段；其他失败原因单独记录。

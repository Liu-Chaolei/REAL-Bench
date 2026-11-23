# 用户主动插话被错误让话：最小机制修复计划

状态：已实施。日期：2026-11-22。具体改动和离线结果见 [实施记录](user-yield-fix-results.md)。以下保留原设计范围。

## 1. 目标与范围

修复 0760 中用户主动插话后，只播放约 1–2 秒就被轮换策略清空剩余语音的问题。

实现限定为两项：

1. 当前讲话时长和打断门控使用与说话计数器一致的语义。
2. `overlap_initiator=None` 明确表示没有确定的当前重叠，不再进入“对方打断用户”的阈值分支。

通过正常轮换机制修复，不增加运行时一致性审查、裁判、额外模型请求、异常拦截或自动重试。开发阶段只做针对性的离线检查。

修复后，用户仍然可以正常让话。当前配置的主动插话阈值 25 ticks、被打断阈值 5 ticks 保留；在 0.2 秒 tick 下分别相当于 5 秒和 1 秒。它们仍按已有轮换计数语义使用，并非新增“每次用户开口必定保证 5 秒”的规则。

## 2. 已确认的原因

播放适配器提供两个不同但都合法的字段：

- `contains_speech`：本次输出区间内是否出现过语音，用于保留实际语音证据。
- `speaking_now`：截至输出区间末尾是否仍有语音，用于当前轮换状态。

因此一个区间可以是 `contains_speech=True, speaking_now=False`。它可能包含一句话的末尾或一次短暂停顿，并非数据损坏。

当前代码的冲突：

1. 用户的 `speech_detection()` 调用 `is_speaking()`，读取 speaking_now；对方当前讲话计数会归零。
2. `input_ongoing_speech_duration()` 却逐块读取 contains_speech，仍算出对方持续说话。
3. `input_interrupt()` 因此仍认为发生打断。
4. `overlap_initiator` 根据讲话计数返回 None。
5. 策略只有 self 与 else 两条分支，把 None 隐式当成 other，使用较短阈值。
6. 策略返回 stop_talking；用户播放逻辑清空尚未发出的语音队列。

0760 三处现场均已离线复现：

| 起始 / 停止 tick | 输入 contains_speech / speaking_now | 缓冲计算的持续语音 | 实际选择 |
|---|---|---:|---|
| 359 / 364 | True / False | 5 ticks | when_interrupted，停止 |
| 389 / 396 | True / False | 7 ticks | when_interrupted，停止 |
| 409 / 418 | True / False | 9 ticks | when_interrupted，停止 |

证据：[根因说明](../data/simulations/qwen-gaia2-0760-neutral-user-20261122-160547/user-yield-root-cause.md)、[最小复现状态](../data/simulations/qwen-gaia2-0760-neutral-user-20261122-160547/user-yield-root-cause-reproduction.json)。

## 3. 修复后的状态约定

| 用途 | 采用的信息 |
|---|---|
| 当前是否还在讲话、当前连续讲话、是否启动插话判断 | `is_speaking(chunk)` |
| 一个历史区间内是否确实出现过语音 | `contains_speech` / `speech_intervals` |
| 选择主动插话还是被打断阈值 | 明确的 self / other；None 不采用任何让话阈值 |
| 是否继续播放用户已经生成的发言 | 没有有效打断时返回 keep_talking，由现有播放队列继续输出 |

现有 `tau2.voice.audio_activity.is_speaking()` 已实现向后兼容：有 speaking_now 就使用它，没有该字段时退回 contains_speech。复用这个函数，不再新增第三种语音状态或重新计算 VAD。

## 4. 修改一：统一“当前持续讲话”计算

文件：`src/tau2/agent/base/streaming.py`。

### 4.1 修改 input_ongoing_speech_duration

保留当前从输入缓冲末尾向前计数的方式，只将每块的判断由直接访问 contains_speech 改为调用 `is_speaking(chunk)`。

示意：

```python
ongoing_ticks = 0
for chunk in reversed(self.input_turn_taking_buffer):
    if isinstance(chunk, EnvironmentMessage) or not is_speaking(chunk):
        break
    ongoing_ticks += 1
return ongoing_ticks
```

同时更新方法文档，明确返回“截至当前仍在持续的讲话块数”，不是“最近多少块曾经包含过语音”。

### 4.2 input_interrupt 继续复用这个方法

保持现有 `self.is_talking and self.input_ongoing_speech_duration() > 0` 结构。

最新块 speaking_now=False 时，ongoing 返回 0，打断门控自然为 False。这是修复的主要路径，不需要额外的断言或检查器。

### 4.3 历史语音统计保留原语义

`input_total_speech_duration()` 继续读取 contains_speech。该方法用于“之前是否收到过语音”等历史判断，不能跟着改成当前状态，否则已经听到的语音可能被错误忽略。

同样不改写保存消息中的 contains_speech，不移除 speech_intervals，不丢弃 ASR 片段，不清空输入历史来制造“没有打断”。

## 5. 修改二：显式处理没有当前重叠

文件仍为 `src/tau2/agent/base/streaming.py`，函数 `basic_turn_taking_policy()`。

在用户仍有待播放语音、并进入打断处理时，先读取 `overlap_initiator`：

- None：继续说话；不选择 when_interrupted 或 when_interrupting 阈值。
- self：按现有主动插话阈值处理。
- other：按现有被打断阈值处理。

示意：

```python
if state.input_interrupt():
    initiator = state.overlap_initiator
    if initiator is None:
        return "keep_talking", "No current overlap"
    if state.delivering_tool_result_speech:
        # 保留原有结果播报的阈值覆盖，但仅用于已确认的重叠。
        active_threshold = yield_threshold_when_interrupting
    elif initiator == "self":
        active_threshold = yield_threshold_when_interrupting
    elif initiator == "other":
        active_threshold = yield_threshold_when_interrupted
    # 继续使用现有时长、check_threshold 和动作返回路径。
```

这不是运行时错误检测：None 本来就是合法状态，为它定义正常行为即可。不记录错误、不中断题目、不重新查询模型。

现有 backchannel 不可被打断的优先分支保持原来的位置。实际的工具结果播报遇到重叠时，仍沿用较高阈值；不会借本次修改让结果播报永久不能被打断。

## 6. 停顿与重新开口的最小处理

本次沿用现有连续讲话计数：

1. 用户主动插话期间双方继续讲话，使用 self 对应阈值。
2. 对方的当前讲话状态变为 False，本次重叠结束，用户继续播放。
3. 对方之后重新开口，按新一段当前讲话重新累计；若用户仍在讲话，可被识别为新的 other 重叠，再适用原阈值。

不为本次修复新增“主动插话身份永久锁定”、停顿滞回、冷却窗口或新的超时配置。是否需要跨短暂停顿保持同一次话轮，可以后续根据独立证据讨论；它不是修复这三次 None 误判的必要条件。

也不把“只说了 1 秒”一概判为错误。真正发生持续的新打断时，用户按原配置让话仍然合理。本次解决的是对方当前已无语音，却用历史含语音标记触发让话。

## 7. 相关调用位置与影响

已核对 `input_ongoing_speech_duration()` 的调用：

- 基础策略判断当前打断。
- 基础策略决定是否执行插话/附和回调。
- 用户异步路径判断当前是否继续监听。
- 用户构造 `generation_origin` 时区分普通回复和插话。
- 等待原因和当前讲话日志。

这些位置都在表达“当前持续讲话”，统一方法语义后应一起生效。不需要在各调用点再复制 speaking_now 检查。

`input_total_speech_duration()` 用于已收到内容、等待和可选的历史重叠丢弃分支，保留历史语义。

共享代码还服务文本或不提供 speaking_now 的参与者。它们会由现有 helper 回退到 contains_speech；需要用一项离线兼容检查确认，不能为了兼容另建供应商分支。

若实现时发现有真正以“历史区间语音”为目的的 ongoing 调用，应当按用途处理那个调用，而不是让 ongoing 同时承担两套语义；避免扩大到全部语音指标或转写代码。

## 8. 文件范围

预期修改：

1. `src/tau2/agent/base/streaming.py`：导入已有 helper、修正 ongoing 计算、明确 None 分支和对应文档。
2. `tests/test_streaming/test_turn_taking_policy.py`：用现有 fixture 增加小规模回归覆盖。
3. 如需要验证实际队列行为，在现有 `test_voice_streaming_user_simulator.py` 中加一项离线动作检查。
4. 一份实施记录，说明行为与检查结果。

预期无需修改用户提示、人设、Agent、模型、ASR/TTS、音频播放队列实现、场景时钟、评分器或镜像。不新增专门配置项，不改变当前 6000 ticks 等运行参数。

`_perform_turn_taking_action()` 原本在 stop_talking 时清空未播语音的行为保留。修复的是错误发出 stop_talking 的条件；不要改为“虽然应该停止但偷偷继续播”，也不要自动续播已被正常打断的旧发言。

## 9. 开发阶段的轻量验证

### 9.1 一组状态序列测试

复用现有 StreamingState 和 policy fixture，覆盖：

- **0760 三个现场**：最新块 contains_speech=True、speaking_now=False；修复后 ongoing=0、input_interrupt=False、动作 keep_talking，输出队列保持待播。
- **持续主动插话**：真实 self 重叠时，低阈值不会被采用；达到原高阈值时仍能让话。
- **真正被打断**：真实 other 重叠且达到原低阈值，仍返回 stop_talking。
- **None 分支**：单独构造合法“没有确定当前重叠”的输入，不再隐式按 other 处理。
- **停顿后重开口**：停顿时继续说，对方重新开口后按新的连续讲话计数判断。
- **旧消息兼容**：不含 speaking_now 时仍按 contains_speech；历史 total 计数保留区间内的语音。

已有 backchannel 和工具结果播报测试直接复用，不另建一套验收框架。修复后运行这个目标测试文件即可。

### 9.2 一项队列检查

用已有用户 fixture、假的待播音频块和固定状态，不调用 LLM/TTS：在停止误判的现场，策略选择 keep_talking 后只取出下一块，剩余队列不被清空。确实返回 stop_talking 的场景仍按原逻辑停止。

不以完整播放一段真实 TTS、完整跑完 0760 或任务奖励提升作为这项机制修复的必需验收。

### 9.3 工程检查

仅运行修改文件对应的 lint/格式检查、目标离线用例和差异检查。通过后停止扩大检查范围；有新的失败再针对原因处理。

上述断言只存在开发测试目录中。生产轮换函数里没有“若两个字段不一致则报错”的逻辑，因为这两个字段的不同值本来就可能正确。

## 10. 生产运行不增加的负担

- 不新增模型裁判、语音检查服务或 ASR 重转写。
- 不新增逐 tick 状态审计、一致性断言、违规标记或评分拦截。
- 不新增播放前验证、每题验收或自动重跑。
- 不重新计算音频能量或遍历录音寻找“是否真正停止”。
- 不新增监听线程、定时器、缓冲副本或身份持久化状态机。

运行成本只是复用已有字段读取和一个明确的条件分支。原来的缓冲计数仍按原方式执行，没有额外扫描层。

## 11. 实施顺序与交付

1. 修改 ongoing 语音判断及其文档。
2. 修改 None 的正常分支，保留原阈值与动作接口。
3. 加少量状态回归和队列检查，用固定输入离线执行。
4. 查看 0760 三个现场的修复前后动作，保存简短结果。
5. 更新实施记录，说明下一次评测直接使用新的主机端轮换逻辑，无需重建 Agent 镜像。

完成标准：三个已确认现场不再错误触发 stop_talking；真实重叠仍能按原配置让话；实际语音历史保留原语义。

不承诺所有长对话都会变短，也不承诺 0760 会开始执行业务。前台能力判断和委派失败是另一个问题。

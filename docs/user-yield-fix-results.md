# 用户主动插话被错误让话：实施记录

## 实现

修改集中在 `src/tau2/agent/base/streaming.py`：

1. `input_ongoing_speech_duration()` 使用已有 `is_speaking()`，与用户的当前讲话检测采用同一语义。最新块 `speaking_now=False` 时结束当前持续讲话计数。
2. `basic_turn_taking_policy()` 对 `overlap_initiator=None` 返回 `keep_talking / No current overlap`，不再隐式进入“对方打断”的阈值分支；工具结果播报的阈值覆盖也只用于已确定的重叠。

`input_total_speech_duration()` 仍统计 `contains_speech`，历史语音和实际播放证据保留原含义。缺少 speaking_now 的旧消息由已有 helper 回退到 contains_speech。

用户的播放队列动作没有修改：正确的 keep_talking 取下一块，正确的 stop_talking 仍清空剩余语音。现有 5 秒主动插话和 1 秒被打断配置、backchannel 和结果播报优先规则保持原逻辑；没有新增身份锁定、停顿平滑或计时配置。

这是主机端共享轮换机制的修复，下次运行会使用新代码，无需重建待测 Agent 镜像。它不修改 Agent 的能力判断或任务委派，也不保证业务奖励提高。

## 针对性离线检查

共 20 项通过，耗时约 0.16 秒：

- 0760 的 5、7、9 个缓冲语音块现场，均在 speaking_now=False 时继续用户发言，且保留历史语音块计数与待播队列。
- None 不再采用被打断阈值，包括结果播报状态。
- 持续主动插话仍采用高阈值，达到阈值仍能停止。
- 停顿结束后对方重新开口，重新累计当前讲话，并可按低阈值正常让话。
- 缺少 speaking_now 的旧消息兼容。
- 已有普通动作和工具结果播报用例，以及 backchannel 优先级。
- 实际用户队列动作：误判现场只取下一块、保留余下块；真实打断仍按原逻辑清空队列。

运行命令：

```bash
PYTHONNOUSERSITE=True UV_FROZEN=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
LD_LIBRARY_PATH="$PWD/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
.venv/bin/python -m pytest -q \
  tests/test_streaming/test_turn_taking_policy.py \
  tests/test_streaming/test_voice_streaming_user_simulator.py \
  -k 'CurrentSpeechTurnTaking or DeliveringToolResultSpeech or BasicTurnTakingActions or current_overlap_controls_user_queue'
```

另外 27 个不相关用例未选中。修改的三个 Python 文件通过 Ruff lint/format 检查，相关差异通过 `git diff --check`。

## 三个现场回放

从已有定位记录读取固定状态，直接调用修复后的策略，没有修改原始轨迹或重新运行评测：

| 原起始 / 停止 tick | 修复前 | 修复后 | 历史含语音块数 |
|---|---|---|---:|
| 359 / 364 | stop_talking | keep_talking | 5 |
| 389 / 396 | stop_talking | keep_talking | 7 |
| 409 / 418 | stop_talking | keep_talking | 9 |

回放结果见 [0760-state-replay.json](../build/user-yield-fix/0760-state-replay.json)。这是固定状态下的决策回放，不声称整场对话已重新播放或任务已完成。

## 运行成本与范围

生产代码只增加已有状态 helper 的使用和 None 的普通分支，没有新增审查器、裁判、断言、模型调用、识别请求、音频分析、重试或评分拦截。

没有重跑 0760，没有重新评分历史结果，也没有扩大到完整语音测试套件。工作区已有的音频分块证据改动保留，本次没有将其作为让话修复重新改写。

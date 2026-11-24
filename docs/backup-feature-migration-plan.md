# tau-backup 功能迁移实施计划

状态：机制实现已完成，真实 Qwen smoke 待执行。

已完成的离线工作包括 watchdog 生命周期、Qwen native PCM playback format、streaming observer 接入、stage activation、当前 1000 任务 v3 catalog 和机制测试。

目标是把 `origin/tau-backup` 中当前工作区缺少的 watchdog、Qwen streaming ASR、stage hotwords 和相关配置迁移到 Gaia2-only 工作区。实现以运行时机制为主，不把新逻辑写入 evaluator、reward 或评分公式。

## 1. 固定边界

当前 Gaia2 使用 1000 个任务。backup 使用 1100 个任务，其中额外 100 个是独立的 `__async_*` 任务。

迁移规则：

- 使用当前 `data/tau2/domains/gaia2/tasks.json` 的 1000 个任务；
- 不恢复独立的 async 任务；
- 当前热词目录不生成 async profile；
- 共同的 1000 个任务按当前任务内容重新计算 input hash；
- backup v1/v2 profile 不直接覆盖当前数据；
- 新功能默认关闭；
- 不修改任务成功公式、语音指标公式、reward 或历史结果格式；
- 不新增评测系统 acceptance、二次裁判、自动修复或自动重跑。

当前目录目标：

```text
data/gaia2_stage_hotwords/v3/
  manifest.json
  tasks/<current-task-id>.json  # 1000 个
```

manifest 应明确记录：

```json
{
  "version": "gaia2-stage-hotwords-v3",
  "tasks": 1000,
  "async_tasks": 0,
  "dataset_sha256": "<current tasks.json sha256>",
  "weight": 50,
  "weight_policy": "first-50-cumulative-v1",
  "complete": true
}
```

## 2. 移植方式

不要直接 merge 或 cherry-pick 整个 backup 分支。当前工作区已经完成 Gaia2-only 清理，整分支合并会重新带回旧 multilingual、judge、persistent-task 和旧数据路径。

按功能参考以下提交：

| 提交 | 参考内容 |
|---|---|
| `4961bee` | Gaia2 语音、诊断和 Qwen 适配基础改动 |
| `387f062` | dynamic followup 和 DeepSeek 配置；当前大部分已经存在 |
| `14e1c9da` | streaming ASR、stage hotwords、Qwen TTS |
| `d9d94ff4` | watchdog、full-duplex 长运行控制和测试 |

以当前文件为主体逐项移植，保留当前的结果目录、语言运行时和 scenario mechanism。

## 3. 配置模型

在 [config.py](../src/tau2/config.py)、[asr.py](../src/tau2/data_model/asr.py) 和 Qwen 配置中统一以下字段：

```text
asr_provider
asr_model
asr_hotwords.enabled
asr_hotwords.mode
asr_hotwords.catalog_dir
playback_sample_rate
asr_append_seconds
asr_streaming_timeout_seconds
watchdog
```

规则：

- `user_background` 使用当前普通 hotword builder；
- `gaia2_stage_gt` 只允许 Gaia2 English；
- `gaia2_stage_gt` 使用 weight 50；
- streaming model 才能选择 streaming observer；
- watchdog 默认 `enabled=false`；
- 配置只保存环境变量名和路径，不保存密钥。

配置错误在 Pydantic model 层拒绝，不能等到 provider 已启动后才失败。

## 4. Native PCM 播放链路

当前 Qwen adapter 仍主要走 8 kHz μ-law 播放路径。需要让 streaming observer 真正使用 Agent 的原生 PCM 格式。

涉及：

```text
src/tau2/voice/audio_native/qwen_agent/discrete_time_adapter.py
src/tau2/agent/discrete_time_audio_native_agent.py
src/tau2/voice/audio_native/tick_result.py
src/tau2/voice/audio_native/audio_converter.py
```

需要引入明确的 `playback_audio_format`，并让以下路径使用同一个实际格式：

- provider 输出；
- playback queue；
- output audio tap；
- playback receipt；
- `speech_intervals`；
- `media_timeline`；
- observer 输入。

规则：

- 普通 Qwen observer 继续使用现有 telephony 路径；
- streaming observer 使用实际 Agent PCM sample rate；
- 不把 24 kHz PCM 当作 8 kHz μ-law；
- sample rate 不匹配时立即报错；
- 只有显式配置时才允许重采样；
- caller 输入保持现有独立音频格式。

时间来源固定为：

```text
音频接收时间 -> playback queue 开始时间 -> 实际播放区间 -> 有效发声区间
```

语音指标读取媒体时间线和 `speech_intervals`，不读取 provider 自己的 speech flag。

## 5. Qwen streaming ASR

文件：

```text
src/tau2/voice/audio_native/qwen_agent/qwen_streaming_transcription.py
src/tau2/voice/qwen_asr.py
```

主要对象为 `QwenStreamingPlayedAudioTranscriber`。每个真实 speech episode 建立独立 session：

```text
idle
  -> speech_started
  -> connecting
  -> running
  -> finishing
  -> completed / failed
```

机制要求：

- 只有实际播放的语音才进入 session；
- 排队但未播放的音频不进入 ASR；
- 每个 append 带 playback start/end 时间；
- 播放取消时 flush 当前 session；
- 连续静音达到阈值时 flush；
- `finish-task` 等待 provider final event；
- provider 错误不暴露 URL、response body 或密钥；
- 每个 session 固定创建时的 sample rate 和 vocabulary；
- session 失败不影响下一段新 speech episode。

transcript 规则：

- 只接受连续的 `sentence_end=true` 前缀；
- 已确认 sentence 不允许被修改；
- snapshot 可以是累计文本；
- user history 只消费新增 fragment；
- final merge 保留 session 时间，不把 session 时间误写成 tick 时间。

## 6. Stage hotword loader

文件：

```text
src/tau2/user/stage_asr_hotwords.py
```

loader 只负责读取和校验，不调用 extraction model。

校验内容：

- task ID；
- 当前 tasks digest；
- profile input digest；
- prompt digest；
- stage 数量和顺序；
- term 是否出现在当前 stage source；
- 累计 vocabulary；
- weight policy；
- profile hash；
- manifest hash。

stage input 只包含：

- 当前 stage 的用户 request；
- reference tool arguments；
- reference agent replies。

运行时不扫描整个数据库、不访问隐藏实体、不调用 extraction LLM。

## 7. 当前 1000 任务热词目录

backup v1/v2 不能直接复制，因为它们绑定 1100 个旧任务。迁移脚本应从当前任务读取 stage source，再处理 backup 已审核词条：

1. 读取当前 `tasks.json`；
2. 查找对应的 reviewed profile；
3. 重新检查词条是否是当前 source 的 literal substring；
4. 丢弃当前任务不存在的词条；
5. 重新计算 `input_sha256`；
6. 重新写入 v3 profile；
7. 生成新的 manifest；
8. 明确排除所有 `__async_*` ID。

authoring 可以离线执行一次，运行时不重新 authoring。

每个 profile 的累计 vocabulary 仍按 stage 增长。前 50 个累计词使用 weight 50，后续词使用 weight 5 时，需要在 schema 和 loader 中保持一致。

## 8. Stage activation

调用链：

```text
ScenarioStages.current
  -> actual user speech chunk
  -> FullDuplexOrchestrator._activate_observer_stage()
  -> agent.set_observer_stage(stage)
  -> adapter.set_observer_stage(stage)
  -> transcriber.activate_stage(stage)
```

规则：

- 文本生成不触发；
- 队列入队不触发；
- 静音 chunk 不触发；
- 第一段非静音用户音频实际开始发送时触发；
- stage 只能单调递增；
- 切换前结束旧 ASR session；
- 新 session 固定新 vocabulary；
- recheck 使用 fragment 原来所属 session 的 vocabulary。

这只改变 observer 词表，不改变用户文本、Agent prompt、工具调用和任务目标。

## 9. Watchdog

配置对象：

```text
src/tau2/data_model/watchdog.py
```

运行控制器：

```text
src/tau2/orchestrator/watchdog.py
```

heartbeat 在以下位置更新：

- run start；
- tick start；
- tick end；
- tool execution；
- user audio delivery；
- agent playback progress；
- stage transition；
- finalization。

judge 输入只包含：

- 当前任务 request；
- 最近完成的对话 turn；
- 当前活动 turn；
- 最近工具调用；
- pending operation；
- heartbeat 状态；
- 当前 stage。

允许的决策：

```text
continue
wait_one_more_checkpoint
stop_and_evaluate
uncertain
```

行为规则：

- timeout 或非法响应转换为 `uncertain`；
- 后台 heartbeat 线程只设置 stale 标记；
- 真正停止在 orchestrator 安全边界执行；
- 使用正常 finalization；
- 结果只写入 `SimulationRun.info.watchdog`；
- 不修改 reward、任务结果或指标。

## 10. Runner 接入

在 [runner/build.py](../src/tau2/runner/build.py) 中：

1. 识别 `asr_hotwords.mode == "gaia2_stage_gt"`；
2. 在 Agent 启动前加载当前 task profile；
3. 将 profile 传给 observer；
4. 将 watchdog config 和 judge 传给 full-duplex orchestrator；
5. profile 不匹配时在启动前失败。

以下情况直接拒绝准备当前 run：

- profile 缺失；
- task ID 不属于当前 1000-task population；
- input hash 不匹配；
- source 不一致；
- stage 顺序非法；
- vocabulary 或 weight 非法。

这是运行前配置保护，不是 evaluator acceptance。

## 11. Qwen TTS

当前 [qwen_tts.py](../src/tau2/voice/qwen_tts.py) 已经有 URL、响应和音频校验逻辑，不直接覆盖 backup 版本。

只补缺失行为：

- retryable `QwenTTSConnectionError`；
- 认证错误与响应格式错误的区分；
- 长文本切分；
- WAV 声道和采样率处理；
- base64 与 URL 音频响应；
- 错误信息脱敏。

保留当前配置模型和 API config 入口。

## 12. 轻量测试

只写机制测试，不写全量评测验收。

watchdog：

- 默认关闭；
- 配置边界；
- heartbeat stale；
- judge timeout；
- stop path；
- summary serialization。

streaming observer：

- 未播放音频不会发送；
- cancellation 丢弃未播放队列；
- silence flush；
- final sentence 顺序；
- snapshot 去重；
- sample rate 校验；
- session vocabulary 固定。

stage hotwords：

- 1000 个当前任务都能加载；
- async profile 数量为零；
- source 改变会拒绝；
- 未 grounding term 会拒绝；
- activation 只发生在真实语音开始；
- recheck 使用原 session vocabulary。

测试使用 fake provider、fake audio queue 和 fake judge，不调用真实 API。

## 13. 单任务 smoke

离线机制测试通过后，只运行一个当前任务的真实 Qwen smoke：

- 使用 streaming Qwen config；
- 使用 v3 catalog；
- watchdog 保持关闭；
- 保存 transcript、media timeline 和 observer summary；
- 检查 stage activation；
- 检查 ASR 只收到已播放音频；
- 检查结果中没有 async profile；
- 检查错误信息不含密钥。

不运行 1000 个真实任务，不做热词准确率统计，不做完整 benchmark 验收。

## 14. 实施顺序

1. 完成 native PCM playback format 和时间线接入；
2. 完成 streaming observer session 状态机；
3. 完成 stage hotword loader 和 v3 catalog；
4. 接入真实 speech boundary 的 stage activation；
5. 完成 watchdog 生命周期和结果记录；
6. 补当前代码结构的机制测试；
7. 更新 Qwen 配置、脚本和文档；
8. 做单任务真实 smoke；
9. 整理迁移文件形成独立提交。

## 15. 完成标准

完成迁移需要满足：

- 当前 1000 任务 profile 全部可加载；
- async profile 为零；
- native PCM sample rate 在播放、timeline 和 observer 中一致；
- watchdog 默认关闭且不会影响普通运行；
- stage activation 只由真实语音触发；
- streaming observer 只处理实际播放音频；
- 轻量机制测试和 Ruff 通过；
- 单任务 smoke 能产生完整 provenance；
- 不修改 evaluator、reward、任务评分和历史结果。

最终依靠 heartbeat、播放边界、profile hash、stage activation 和 session vocabulary 保证行为一致性。评测系统只消费已有 transcript、media timeline 和 provenance。

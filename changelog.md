# 项目较大改动记录

记录 `tau2-bench-backup` 中较大的功能与评测流程改动，后续按记录日期倒序追加。每项说明目的、实际行为、配置、代码入口和验证情况。上游项目的发布记录见 [CHANGELOG.md](CHANGELOG.md)。

## 2026-11-24：增加可选的全双工长任务 watchdog

- 新增 `WatchdogConfig`，默认关闭；旧配置和原有 `max_steps` 行为保持不变。Qwen Agent 配置可以通过 `watchdog.enabled=true` 启用，默认在 6000 tick 检查，之后每 3000 tick 检查，默认总上限为 12000 tick。
- 在检查点把有限的近期 user/agent 对话、工具调用/结果、pending 状态、场景时间和 heartbeat 阶段交给当前 communicate judge 使用的同一套 LLM 配置。结构化结果支持 `continue`、`wait_one_more_checkpoint`、`stop_and_evaluate` 和 `uncertain`；judge 超时或格式错误按 `uncertain` 继续。
- watchdog 输入已改为事件摘要：给 LLM 的内容包含当前环境时间、阶段状态、带起止时间的对话轮次、当前未完成语音、最近 20 条工具操作和显式 heartbeat 存活状态，不再直接拼接重复的每 tick 累计文本。用户语音尚未结束时由程序侧强制继续，避免把用户正在追问误判为 Agent 重复卡死。
- FullDuplexOrchestrator 增加基于 `time.monotonic()` 的 heartbeat，记录 tick、参与者调用和工具等待阶段。失去 heartbeat 时请求以 `watchdog_stall` 终止，并复用现有 finalization/evaluation 流程；结果把 checkpoint 判断保存在 `simulation.info.watchdog`。
- `watchdog_stall` 被视为可评测终止原因，ENV、execution、voice 等指标仍可对已有轨迹评分。配置与实现入口分别为 [watchdog.py](src/tau2/data_model/watchdog.py)、[orchestrator/watchdog.py](src/tau2/orchestrator/watchdog.py) 和 [full_duplex_orchestrator.py](src/tau2/orchestrator/full_duplex_orchestrator.py)。
- 当前版本使用进程内 heartbeat 和正常收尾路径；它不包含外部 supervisor 的强制杀进程或中途 trajectory checkpoint。API 阻塞仍依赖已有请求 timeout 返回控制权后再进入 watchdog 收尾。
- 验证：watchdog 专项 2 项通过；相关 outcome、benchmark runtime、orchestrator 和 voice prompt 回归共 54 项通过。全量 pytest 本次未完成，未产生失败报告。

## 2026-11-23：为流式 ASR 累计快照增加非重复文本视图

- 流式 observer 的每个 tick 仍原样保存 `live_transcript.snapshot.text`，用于审计和复现；评测、显示和指标路径不再把这个累计全文当作增量拼接。
- 新增 [streaming transcript view](src/tau2/utils/transcript/views.py)：有 Live ASR 证据时只读取本 tick 新释放的 `fragments`，没有 Live 证据时保留旧 provider 的 `content`/`proportional_transcript` fallback。
- NL assertion、全双工扁平消息、语音交互 transcript、Audacity 标签、控制台/Markdown review、认证/用户 review、user silence check 等路径已切换到该视图。原始 tick、快照、fragment 时间和 ASR 证据不被修改。
- 回归验证覆盖累计快照、重复静音 tick、无 Live metadata 的旧轨迹和既有 Live 分组逻辑；task 0085 的 NL 文本从约 1203 万字符降为约 6764 字符，语音 interaction 的 `extract_agent_segments` transcript 从约 319 万字符降为约 2700 字符。

## 2026-11-22：全量复查旧热词并增量抽取 GT 工具返回词，启用 v2

- 使用 `deepseek-flash` 处理全部 1100 条任务、3300 个阶段，输入仅为旧词短列表及本阶段 GT `tool_response.content`。困难完整专名尽量保留，带变音符号的专名优先；不凑满上限，过滤普通短名、通用词及简单机构词。原始二进制附件不送入 LLM，正常文本保留，长文本分块重叠处理。
- [v2 词表](data/gaia2_stage_hotwords/v2/README.md) 保留旧词到原任务文本/GT 的来源链，并嵌入工具返回的引用文本、reference 事件及阶段、返回值哈希和逐条 profile 哈希。评测启动不依赖 209 MB 的完整 GT dump，也不会重跑 GT 或调用 DS。完整 dump 与逐次 API 请求仍被 Git 忽略。
- 新增 [全量脚本](scripts/review_gaia2_stage_hotwords.py) 和 [prompt](scripts/gaia2_toolresponse_hotwords_prompt.txt)：支持 8 路并发、逐任务/分块断点复用、校验纠错、失败任务分阶段重试，以及让 DS 定向复核被漏选的带变音符号完整人名。未手工填写热词。
- 只以实际保留的当前/之前阶段词语做连续词序列覆盖过滤，修正实验中“已删除的旧词或未来词仍抑制新词”的问题。每阶段词表真正累计，空阶段不会清除前面已启用的词。
- 默认 catalog 切换到 `gaia2_stage_hotwords/v2`，保留 v1 供显式回退。按阶段累计启用顺序，前 50 个词使用超级热词权重 50，超过后新增的词使用 5，与来源无关（Qwen 不支持 49）；当前 realtime 配置 `max_terms` 改为 2000，同时仍严格限制权重 50 的词最多 50 个。全量抽取后曾误按旧词/工具返回来源分配权重，现已离线修正全部词表、汇总和哈希，未改变词语或重新调用 DS。
- 全部完成并通过运行时加载校验。共消耗 6,321,231 tokens（含纠错/定向复核）；全局去重 3635 词，每任务累计平均 9.29 词。少数长列表任务较大，最大 567 词，仍满足总量及超级热词限制。统计见 [summary.json](data/gaia2_stage_hotwords/v2/summary.json)，逐条验证见 [VALIDATION.json](data/gaia2_stage_hotwords/v2/VALIDATION.json)。
- 验证：1100 条词表均通过新版运行时加载器；6174 处来源证据与完整 GT dump/旧词表逐条匹配；定向复核后，旧词中未被完整长词覆盖的带变音符号完整人名漏项为 0。40 项回归测试通过，包含当前 realtime 配置加载全部 v2 词表、v1 回退、阶段隔离、第 50/51 个词的权重边界、引用篡改拒绝、短词覆盖以及流式 observer。此处验证词表和接入规则，没有新跑完整语音 task 评测。

## 2026-11-22：将用户端 TTS 从 MiMo 切换为 Qwen-Audio-TTS

- 当前 realtime Qwen Agent 配置的用户模拟器 TTS 改用 DashScope `qwen-audio-3.1-tts-flash`，与 observer Qwen ASR 共用 `QWEN_API_KEY` 和 `QWEN_BASE_URL`；不再读取 `MIMO_API_KEY`。
- 新增 [Qwen TTS 适配器](src/tau2/voice/qwen_tts.py)：调用 DashScope `SpeechSynthesizer` 非流式接口，下载返回的 WAV，验证 PCM16、转单声道，并归一化为 tau 使用的 16 kHz PCM。超过 Qwen 单请求 600 字符限制的文本会按标点或空格切段后拼接。
- 英文 persona 映射为 `Andy_v3.1`（mamadou_diallo）和 `Abby_v3.1`（priya_patil），语言参数由 `en` 转为 `English`。
- 旧的 MiMo TTS 配置和实现仍保留，其他显式选择 MiMo 的路径不受影响；只有当前 realtime 配置的 `tts.provider` 改为 `qwen`。
- 验证：当前 Qwen key 的最小 `Hello.` 请求返回 HTTP 200；完整适配器调用成功下载音频并输出 16 kHz PCM16。Ruff 和 Python 编译检查通过；已有 MiMo 测试 6 项通过，剩余 1 项因集群缺少 `libasound.so.2` 导致 PyAudio 导入失败，与本次改动无关。

## 2026-11-22：将固定分阶段热词接入 user 端 observer ASR

- 当前 realtime 配置启用 `asr_hotwords.enabled=true`、`mode=gaia2_stage_gt`、`weight=50`、`max_terms=50`。从现有 1100 条任务词表按完整 task ID 加载，运行时不再调用 DS 抽取，也不扫描环境；默认目录为 `TAU2_DATA_DIR/gaia2_stage_hotwords/v1`。
- 启动前验证任务 source/GT 哈希、prompt/model 版本、同阶段原文引用、累积词表和上限。缺失、过期或不一致的词表会在 agent 启动前报错。模式要求 English Gaia2 与 Qwen 流式 observer；旧的 `user_background` 模式仍保留。
- stage0 初始启用自己的词。新阶段请求开始实际语音传输时，才启用此前与当前阶段词语的并集；只生成或排队请求时不提前启用。兼容 metadata 位于首个静音片段、实际语音稍后开始的情况。
- 阶段切换先收尾旧音频片段，再让新会话使用新词表。**每个会话创建时固定自己的阶段、实际词表及哈希**；旧会话即使排队或收尾较慢，也不会使用未来阶段热词。录音复核沿用所引音频当时的阶段词表。
- 热词只进入 user 端 observer ASR，不进入前后端 agent 的 prompt、工具参数或容器环境。记录选定任务的 `observer_hotwords.json`，并在 observer summary 中保存启用时刻及各会话的实际词表。
- 237 项回归测试通过，包含全部 1100 条词表的运行时加载校验，以及延迟建连、阶段切换、旧录音复核、实际语音传输边界、动态任务和现有音频路径。
- 用保存的原生 24 kHz 录音经正式 agent/adapter 与真实 Qwen ASR 做三阶段重放：3 个 ASR 会话、170 个 tick，逐阶段实际词表完全匹配。task 0007 依次为 `Lysverket`，再增加 `Kaida Rao`，再增加 `Åse`；本例三次均正确识别了 Lysverket。这是 ASR 通路验证，没有重新执行完整 Gaia2 任务或 judge。

代码入口：[词表加载](src/tau2/user/stage_asr_hotwords.py)、[数据结构与权重校验](src/tau2/data_model/asr.py)、[运行构建](src/tau2/runner/build.py)、[阶段传输边界](src/tau2/orchestrator/full_duplex_orchestrator.py)、[ASR 会话](src/tau2/voice/audio_native/qwen_agent/qwen_streaming_transcription.py)。本地验证：[报告](data/simulations/gaia2-stage-hotwords-main-20261122/REPORT.md)、[实际请求词表与转写](data/simulations/gaia2-stage-hotwords-main-20261122/VALIDATION.json)。

## 2026-11-22：完成 1100 条 Gaia2 任务的分阶段热词抽取

- 使用 `deepseek-flash` 和此前确认的筛词 prompt，离线处理全部 1100 条任务，包括 100 条 async 变体，共 3300 个阶段。材料只包含各阶段 source request、reference 工具参数字符串和 reference 回复，不扫描环境快照。
- 结果保存在 [data/gaia2_stage_hotwords/v1](data/gaia2_stage_hotwords/v1/README.md)：逐任务 JSON 保留词语、类型、来源与理由；每阶段同时记录新增词与至该阶段为止的累积词表。权重统一为 50，累积最多 50 词，不提前启用未来阶段词语。
- 新增 [批处理脚本](scripts/extract_gaia2_stage_hotwords.py) 和 [固定 prompt](scripts/gaia2_stage_hotwords_prompt.txt)，支持并发、逐任务断点复用、模型纠错、原始调用记录及 `--verify` 离线全量复核。JSON 重复字段、重复词、邮箱和引用不符会被拒绝；最后 1 条持续多阶段格式错误的任务改为每阶段独立调用 DS，随后按同一规则合并校验。
- 全部 1100 条完成并通过代码校验；全局去重后 3281 个词，每任务最终累积中位数 7 个、最多 49 个。8 条任务为空词表。386 条任务有过重试，保存 1609 次 API 尝试，未手工填词。
- [汇总](data/gaia2_stage_hotwords/v1/summary.json)、[TSV 便览](data/gaia2_stage_hotwords/v1/vocabularies.tsv)、[全量验证](data/gaia2_stage_hotwords/v1/VALIDATION.json) 均已生成。请求和原始回答保存在被 Git 忽略的本地 `data/simulations/gaia2-stage-hotwords-1100-20261122/`。
- 此批处理完成时只产出词表，尚未启用主流程；后续接入见上方同日记录。

## 2026-11-21：原始 PCM 播放与 ASR 直通，移除输出侧的 8 kHz 中转

### 原因

此前 Qwen Realtime 输出先转成 8 kHz μ-law 播放音频，observer 再解码并升到 16 kHz。8 kHz 是框架默认的电话音频格式，便于复用既有输入输出接口；**统一 tick 时钟并不要求使用 8 kHz**。降采样丢失的高频信息也无法通过后续升采样恢复。

### 实际改动

- 将输入与输出格式分开。user 输入继续使用其独立配置；当前 Qwen Realtime 的 **24 kHz、16 位、单声道 PCM 输出直接进入播放缓冲、录音和流式 observer ASR**，客户端不再对这条输出链路做降采样、μ-law 编解码或升采样。
- 每个 tick 按输出采样率和样本宽度计算字节数：200 ms 的 24 kHz PCM16 输出为 9,600 字节，同期 8 kHz μ-law 输入为 1,600 字节，两者仍对应相同的 200 ms 时间轴。
- `TickResult.playback_audio_format` 记录实际输出格式，缓冲截取、静音填充、消息元数据和录音使用该格式。`agent_output.wav` 直接保存原生 PCM。
- **只把本 tick 实际播放的音频送入 ASR**。模型提前生成的音频继续留在播放缓冲；打断时丢弃未播放部分，并在发送新回复前结束旧 ASR 片段。不会因直接使用原始音频而提前泄露后续内容。
- ASR 请求的 `sample_rate` 使用音频实际采样率，PCM 二进制内容保持不变。2 秒 append、最终确认后提交全文、不扣三个词、每 tick 累计快照等机制继续保留。
- 显式录音复核的缓存截取也按实际采样率换算。复核使用的文件 ASR 如需 16 kHz，只做一次从原采样率到 16 kHz 的转换，不经过 8 kHz 电话中转。
- 修正全双工双声道录音导出：双方采样率不同时，将低采样率声道升到较高采样率后再拼接，保留高采样率的 agent 声道，避免格式报错和时长错位。

当前配置新增 `playback_sample_rate: 24000`，必须与模型音频事件声明的实际采样率一致；不一致时报错，而不静默转换。原有文件 ASR、MiMo/Deepgram 配置仍保留原路径。既有 8 kHz 录音无法恢复成原始高采样率录音。

### 验证与证据

- 191 项主流程、ASR、时钟、缓冲、打断和旧路径回归，以及 12 项音频处理/混合采样率录音导出测试通过，共 203 项。
- 使用当前前端配置的 `qwen-audio-3.1-realtime-flash` 新生成 10 秒原生 24 kHz 音频，再通过正式 agent/adapter 与真实 `qwen-audio-3.1-asr-flash-streaming` 重放。共 58 个 tick、1 个 ASR 会话、6 次 append。
- 逐字节验证模型原始 PCM、实际播放录音和 ASR 输入一致。user 历史与评分文本仍对应已确认全文。
- 此次是音频链路验证，不是重新执行完整 Gaia2 业务任务。热词仍关闭，地点 Lysverket 仍被 ASR 识别为 Lees Ferkit；保真传输不等于专有词识别必然正确。

代码入口：[adapter.py](src/tau2/voice/audio_native/adapter.py)、[tick_result.py](src/tau2/voice/audio_native/tick_result.py)、[Qwen adapter](src/tau2/voice/audio_native/qwen_agent/discrete_time_adapter.py)、[流式 ASR](src/tau2/voice/audio_native/qwen_agent/qwen_streaming_transcription.py)、[agent 录音与消息](src/tau2/agent/discrete_time_audio_native_agent.py)、[双声道导出](src/tau2/voice/utils/conversation_builder.py)。

本地证据：[原始 24 kHz 音频](data/simulations/qwen-native-playback-20261121/original-realtime-24khz.wav)、[播放录音](data/simulations/qwen-native-playback-20261121/audio/taps/agent_output.wav)、[验证结果](data/simulations/qwen-native-playback-20261121/VALIDATION.json)、[报告](data/simulations/qwen-native-playback-20261121/REPORT.md)。

## 2026-11-21：首次整理当前实现

以下日期为记录日期；dynamic task 的相关改动此前已逐步完成，流式 observer ASR 已接入主流程。

### 1. 基于历史对话生成后续 dynamic task

#### 目的与适用范围

让多阶段任务中的后续请求结合前面的对话，以更自然的衔接、语气和情绪表达提出原有要求。原始 task 文本作为不可修改的业务目标；动态生成改变用户的说法，评分仍使用原任务的工具 GT 和断言。

当前通路用于 **English Gaia2、`qwen_agent` 语音运行**，默认关闭，通过 `dynamic_followup.enabled` 启用。阶段编号从 0 开始：

| 内部阶段 | 用户实际收到的任务顺序 | 默认处理方式 |
| --- | --- | --- |
| `stage0` | 第 1 个请求 | 使用原始文本 |
| `stage1` | 第 2 个请求 | 启用后可动态生成 |
| `stage2` | 第 3 个请求 | 启用后可动态生成 |

没有生成新的业务任务或接入状态转移图；后续请求仍以对应阶段的 source request 为目标。

#### 生成流程

1. **判断用户感知的完成情况与情绪。** 单独调用 LLM，只传此前的 user/agent 对话，不传工具 GT、环境状态或 source task。判断的是“用户认为前一项工作怎么样”，不代替环境评分。
2. **生成当前请求。** 传入当前 source request、此前对话与工具观察、已释放的阶段信息，以及上一步的 `status`、`affect`。生成器直接参考 source request，不接收工具 GT。
3. **进行本地规则检查。** 验证结构化输出，并检查 source 中的邮箱、引号内文本和数字串等受保护值是否保留。匹配允许大小写及部分标点、空白差异，语义是否等价还需下一步核查。
4. **独立调用 LLM 核查。** 同时提供 source、生成文本、历史和当前阶段工具 GT，检查要求是否完整、是否增加任务、是否泄露信息、指代是否一致，以及历史事实是否有依据。
5. **使用通过核查的文本合成语音。** 失败时按配置重试；仍失败则使用原始 source 文本。

上述三个 LLM 角色是分别发起的调用，使用当前 user 模拟器的模型配置，并不要求三个不同模型。上下文分类调用固定 `temperature=0`；生成与核查继承模型参数。

#### status、affect 与表达约束

- `status` 为固定枚举：`completed`、`partial`、`incomplete`、`failed`、`timed_out`、`abandoned`、`unknown`。优先依据用户最近明确的表态，不仅凭 agent 声称完成就推定成功。
- `affect` 由 LLM 自由填写简短描述，最长 80 字符；prompt 给出示例，但不限定为几个预设情绪。
- 允许根据前一阶段情况调整开场、礼貌程度、急迫程度和情绪，但不能因此增加偏好、事实、截止时间或业务要求。
- 前一阶段未完成或做错时，可以简短表达失望或催促，但必须转向当前任务，不能要求继续修复、重试上一项任务。
- source 中的跨阶段指代必须保留，例如 “that same chat”“this contact”“those listings”。不能将同一个先前对象改成无关的新对象，也不能丢掉这种关联。
- source 中的收件人、标题、文件名、日期、数量、禁止事项等要求必须保留；生成文本可以改变句式和表达顺序。

#### 工具 GT、信息泄露与历史事实核查

工具 GT 只作为 checker 判断操作和参数语义的参考，不要求用户复述内部 ID。**只出现在 GT 或历史工具结果中、而 source 没有给出的答案、地址、联系人详情、数据库 ID 等，不允许被直接写进 dynamic task。** 必要时保留“你刚找到的地址”“那个群聊”这样的自然指代。

checker 还会检查生成文本中的历史事实声明，例如“你已经发出的邮件”“刚改好的名称”“既然地址已经确认了”。当前 source 恰好提供某个值，不能证明上一阶段已经成功；缺乏历史依据的声明写入 `unsupported_historical_claims` 并拒绝该候选。

生成与状态分类默认使用最多 24,000 字符的近期上下文；checker 使用最多 100,000 字符的历史窗口，以保留更多早期证据。这些窗口都有长度限制，不表示无限保留完整历史。

核查输出包括 `valid`、原因，以及缺失要求、新增要求、缺失/额外工具、泄露信息和无依据历史声明等列表。`valid=false` 或任一问题列表非空，都会拒绝候选。

#### 重试、fallback 与记录

- `max_retries=1` 表示最多尝试两轮候选生成和检查。核查不通过或调用出错，会触发重新生成；耗尽后回退到 source request。
- checker 的结构化输出校验另有一次格式校验重试，与上面的候选重生成次数分开。
- 如果仅 status/affect 分类失败，先使用现有规则推断作为 fallback，再继续尝试生成；不直接跳过语义核查。
- 运行内按阶段输入 ID、阶段、source、输入修订号和配置等生成缓存键，命中时复用该次结果，避免相同上下文的重复生成。该缓存不等于跨运行断点续传，也不保证不同完整运行的措辞逐字相同。
- 用户消息的 `raw_data.dynamic_followup` 和 user 运行记录中保存 source、最终文本、核查结果、是否 fallback、status/affect、分类依据、上下文哈希、修订号、调用时间及可用的成本信息。

#### 配置与代码入口

```json
{
  "dynamic_followup": {
    "enabled": true,
    "stages": [1, 2],
    "max_context_characters": 24000,
    "max_retries": 1
  }
}
```

- 配置与输出结构：[dynamic_followup.py](src/tau2/data_model/dynamic_followup.py)。
- 分类、生成、checker prompt 和重试逻辑：[dynamic_followup.py](src/tau2/user/dynamic_followup.py)。
- user 侧阶段接入、缓存、fallback、语音生成与记录：[user_simulator_streaming.py](src/tau2/user/user_simulator_streaming.py)。
- 运行构建与适用范围校验：[build.py](src/tau2/runner/build.py)。
- 对应回归用例：[test_dynamic_followup.py](tests/test_user/test_dynamic_followup.py)。

### 2. 将流式 observer ASR 请求接入主流程

以下为首次流式接入的记录；音频格式部分已由上方同日的原始 PCM 直通改动更新，确认、快照和收尾机制继续保留。

#### 目的与适用范围

将 Qwen observer 的独立片段识别扩展为连续流式识别，让同一段发言在多个音频批次之间保留识别上下文。该 observer 接收的是 **agent 实际播放给 user 的音频**，转写供 user 模拟器及后处理使用；不使用 Qwen 自带的回复字幕代替实际听到的内容。

主流程使用 `qwen-audio-3.1-asr-flash-streaming`。原来的 `qwen-audio-3.1-asr-flash` 文件 ASR 路径仍可通过配置选择。

#### 请求与会话生命周期

1. 在实际播放流中检测到语音后创建识别 task；建连期间保留已播放音频。
2. 使用配置中的 DashScope 区域主机，连接 `/api-ws/v1/inference`，发送 `run-task`，等待 `task-started`。
3. 初版将播放侧的 8 kHz PCM16 转成 16 kHz 单声道 PCM16，使用同一 task 发送二进制音频批次并保留重采样状态；后续已用原始 PCM 直通替换这一步。
4. **最多累计 2 秒后 append**；不是每 2 秒重新发起独立 ASR 请求。最后不足 2 秒的部分也会发送。
5. 连续播放静音达到默认 0.6 秒、实际播放被打断或运行结束时，发送剩余音频，再发 `finish-task`。
6. 继续接收最终识别结果，收到 `task-finished` 后关闭连接；后续出现新的发言时创建新 task。

网络建连、发送、接收与收尾在后台执行，媒体 tick 不等待 ASR 完成。长时间查工具且没有语音时，不新建 ASR 会话；此前发言的会话收尾后关闭。

#### 文本确认规则：不再扣末尾三个词

- `sentence_end=false` 的中间结果只作为诊断记录，不提交给 user LLM。
- 仅将按顺序连续确认的句子并入可见累计文本；后面的句子先确认时，等待前面的缺口补齐。
- 收到 `sentence_end=true` 后提交该句完整文本，**不再额外扣掉末尾三个空格词**。
- 两秒是音频批次的上限，不是文本最终确认的周期。长句可以跨越多次 append，不能保证每两秒都有新的可见文本。
- 不能用“几秒没新输出”当作最终确认；也不能把停止发送音频当作已经发送了静音或结束指令。

这一选择经过截断实验验证：将 task 0007 的实际录音发到“2024”末尾后，保持连接、停止发送约 10 秒，日期句仍为中间结果 `Friday, November 29, 2020`；发送 `finish-task` 后约 0.74 秒，才返回最终结果 `Friday, November 29, 2024`。该结果说明本例需要显式收尾，不代表所有请求都具有相同延迟。

#### 每 tick 快照、user 历史与评分

每 tick 的 `live_transcript.snapshot` 保存：

| 字段 | 含义 |
| --- | --- |
| `version` | 已确认累计文本的版本号 |
| `text` | 截至该 tick 已收到的完整已确认文本 |
| `pending` | 是否仍有尚未完成的识别会话 |

`live_transcript.fragments` 另外保存本 tick 新确认的片段，每个片段带唯一 `event_id` 和对应实际播放时钟的起止时间。

保存轨迹使用累计快照；构建 user LLM 历史时按实际释放 tick 使用新增片段，避免将每 tick 的全文重复拼接。没有新结果时沿用现有文本，累计快照与运行中的 user 观察历史不回填未来文本。user 的输入修订号不会仅因为重复出现同一个累计快照就增加。运行结束后的最终 observer flush 沿用框架的最终记录合并逻辑，结果仅供后处理，不再反馈给已经结束的 user 模拟器。

后处理继续读取已确认片段及其音频对齐信息。ASR summary 保存会话边界、append 范围和音频哈希、中间识别事件、已确认文本版本、usage 与确认延迟。已有录音复核工具仍可调用文件 ASR，其结果作为补充证据，不替换当时的观察历史。

#### 配置、错误处理与当前边界

当前 [realtime 配置](integrations/qwen_audio_agent/config.gaia2.realtime.dynamic.metrics.deepseek-user.json) 已启用以下通路；其中静音与超时参数可省略，使用默认值：

```json
{
  "asr_provider": "qwen",
  "asr_model": "qwen-audio-3.1-asr-flash-streaming",
  "asr_api_key_env": "QWEN_API_KEY",
  "asr_provider_env": "QWEN_AUDIO_REALTIME_PROVIDER",
  "asr_base_url_env": "QWEN_BASE_URL",
  "asr_append_seconds": 2.0,
  "asr_silence_seconds": 0.6,
  "asr_streaming_timeout_seconds": 30.0
}
```

连接、请求发送、启动等待和最终收尾使用超时约束。连接失败、超时或 task 结束后仍有未确认文本时报告错误，不将中间结果冒充最终结果。保留文件 ASR 配置选项，不表示流式故障时会自动切换到文件 ASR。

本次接入复用已有热词接口；当前配置的 `asr_hotwords.enabled` 仍为 `false`。此前“按阶段从 source/工具 GT 提取热词”的方案属于离线实验，尚未随本次流式接入自动启用。ASR 的最终确认也不保证识别正确，姓名、地点等仍可能听错。

#### 代码入口与验证

- 流式会话、音频批次、最终确认与收尾：[qwen_streaming_transcription.py](src/tau2/voice/audio_native/qwen_agent/qwen_streaming_transcription.py)。
- 主流程路径选择、播放后转写与最终 flush：[discrete_time_adapter.py](src/tau2/voice/audio_native/qwen_agent/discrete_time_adapter.py)。
- 配置校验与区域 endpoint：[config.py](src/tau2/voice/audio_native/qwen_agent/config.py)、[qwen_asr.py](src/tau2/voice/qwen_asr.py)。
- 快照结构及 user 历史处理：[transcript.py](src/tau2/data_model/transcript.py)、[streaming.py](src/tau2/agent/base/streaming.py)、[user_simulator_streaming.py](src/tau2/user/user_simulator_streaming.py)。
- 流式回归用例：[test_qwen_streaming_observer.py](tests/test_voice/test_qwen_streaming_observer.py)。

接入时完成 151 项相关回归测试，全部通过；Ruff 和格式检查通过。按项目要求运行 provider suite，其中 132 项因未开启在线测试凭据开关而跳过，不计为通过。

使用 task 0007 保存的 30.4 秒实际播放录音，经正式适配器和真实 Qwen ASR 原速重放：152 个 tick、1 个会话、15 次 append。user 历史视图和评分文本视图均与已确认全文一致，并验证快照不使用未来结果。该验证没有重新运行完整 Gaia2 任务或 judge；热词关闭时，姓名和地点仍有识别错误。

实验与详细记录：

- [停止 append 与显式结束的对比](data/simulations/observer-streaming-stop2024-task0007-20261121/REPORT.md)。
- [主流程接入验证报告](data/simulations/observer-streaming-main-integration-20261121/REPORT.md)。

上述实验产物位于本地 `data/simulations/`，通常不随 Git 提交；实现细节和配置也已记录在 [集成说明](integrations/qwen_audio_agent/README.md)。

# Qwen ASR HTTP 400 定位记录

日期：2026-11-22。定位后已按用户授权完成适配层修复，具体见文末。未重跑整题。

## 结论

已复现一个会重复触发的原因：Qwen 对没有识别出词的短片段返回 `HTTP 400 / CLIENT_ERROR / ASR_RESPONSE_HAVE_NO_WORDS`，当前客户端把这个空识别结果当作致命错误，导致整个 observer 停止，再中断整题。

之前仅凭 HTTP 400 和短暂调用成功，将其描述为“服务不稳定/恢复”，依据不足。当前复现结果指向输入片段和错误语义适配问题，不能用降低并发解决这个分支。

历史异常只保存了 HTTP 状态和通用 code，没有保存 message、Request ID 和每次请求的精确音频边界。因此不能声称已经逐字节重放了历史失败请求，也不能断言历史每一次 HTTP 400 都是同一个 message。

## 实际播放音频上的控制实验

使用单并发复测的 `agent_output.wav`，对应 Agent 实际播放的输出声道，截取最后一段语音的尾部，保持原模型、Key、端点及请求参数。

0069：

- 496.2–497.0 秒的 0.8 秒尾部：HTTP 400，message=`ASR_RESPONSE_HAVE_NO_WORDS`。Request ID：`1515e8f8-12fd-96a2-bf28-83c56bcfb438`。
- 将相同结尾向前扩展至 492.2 秒，共 4.8 秒：HTTP 200，返回 58 个文字字符。Request ID：`18efac3c-b136-98ec-8682-13f3f5f45a78`。

0563：

- 147.0–147.8 秒的 0.8 秒尾部：HTTP 400，同样的 message。Request ID：`3768854e-f827-9dc8-ab7d-e59535322309`。
- 将相同结尾向前扩展至 143.0 秒，共 4.8 秒：HTTP 200，返回 88 个文字字符。Request ID：`855ce6fd-fabd-9962-abf6-155b9000ac3f`。

这些是重分片控制实验，不是已保存的原请求。复现数据保存在本次复测目录的 `asr-http400-reproduction.json`。

调查开始时也试过 `agent_user-input.wav` 上的短片段；该文件是 Agent 接收的用户声道，不能用来重建 observer 的原输入。上述结论最终使用的是重新核对后的 Agent 输出声道。

## 为什么长对话里容易反复发生

`SegmentedPlayedAudioTranscriber` 每累计约 4 秒或遇到 0.6 秒静音就提交一次独立识别；Agent 被打断时还会提前提交未满的片段。

提交前只用 RMS 能量阈值判断是否出现过声音。这不等于片段中必然存在可以独立识别的完整单词。固定长度切片可能剩下半个词、尾音、气声或低幅噪声；模型也可能没有输出可识别词。

客户端没有单独处理这种结果：

1. `qwen_asr.py` 对所有非 2xx 响应直接抛异常。
2. `played_audio_transcription.py::_recognize()` 捕获异常后设置 `self.failure`，退出识别 worker。
3. 下一次 `send()` 或 `drain()` 抛 `observer transcription unavailable`。
4. runner 记录 `infrastructure_error`，保存异常占位 simulation；正常轨迹评分没有完成。

因此一个短片段没有文字，就会终止整道题。长对话产生的切片越多，遇到该分支的机会越多。

## 文档核对与证据边界

- [Qwen-Audio-ASR HTTP API](https://help.aliyun.com/zh/model-studio/qwen-audio-asr-http-api)：支持 WAV Base64 Data URI、16 kHz 和 `language_hints=['en']`。当前请求的这些字段符合文档，同配置下较完整的片段也成功返回。
- [百炼错误码说明](https://help.aliyun.com/zh/model-studio/error-code-start)：`ASR_RESPONSE_HAVE_NO_WORDS` 的解释是最终识别结果为空。该文档条目以 Paraformer 为背景；Qwen 返回同一 message 的 HTTP 400 行为由上述实际请求确认。

这没有证明所有短于一秒的音频都会失败，也没有证明所有较长片段都会成功。关键条件是最终没有识别出词，不能只按时长粗暴丢弃短回答。

## 最小修复方案

在 Qwen 客户端精确匹配本次返回的 `HTTP 400 + CLIENT_ERROR + ASR_RESPONSE_HAVE_NO_WORDS`，将其转为显式的空识别结果，并保留该片段的状态和 Request ID。现有识别循环本来就支持空文本，不生成文字片段后继续下一段即可。

其他 HTTP 400、鉴权、配额、格式、网络等错误继续走原有错误路径。不能将所有 HTTP 400 都忽略，也不能凭模型内部 generated_text 为这个片段补造听到的文字。

这个修复不需要修改 ASR 模型、API Key、并发、时钟或评分器，也不需要新增逐 tick 检查。切片边界优化可另行处理，用于减少尾音断词和识别丢失；不应仅为避免报错而跳过所有短片段。

## 与原先行为的对照

进一步检查历史源码后确认，“识别线程发生异常后记录 failure，随后终止整题”的机制早已存在，不是这次场景时钟注入或错误码记录新增的规则。

原动态评测配置使用 MiMo。`transcribe_mimo()` 接受正常 HTTP 响应中的空字符串，返回 `text.strip()`；共享识别循环对空字符串只是不生成文字片段，然后继续下一段。如果 MiMo 真正返回非 2xx，旧客户端也会抛异常，进入同样的整体失败路径。因此不能说旧版会忽略所有 ASR 错误，也不能在没有原始响应的情况下断言 MiMo 对本次问题片段一定返回空文本。

更早的 Deepgram 路径也明确对空 transcript 执行 continue，对实际服务错误记录 failure。

已核对旧 MiMo 的零售任务 0 成功记录：106 个成功识别片段，对应保存了 106 个非空文字片段。已核对本次随机三题之前成功完成评测的 Qwen 0535 记录：119 个成功识别片段，对应保存了 119 个非空文字片段，且 ASR 模型、4 秒切片、0.6 秒静音和 RMS=80 配置与本次一致。原先的成功记录不能作为“原来有额外的错误忽略机制”的证据；这些记录中没有进入本次已复现的无词 HTTP 400 分支。

本次切换后的适配缺口是：Qwen 用 HTTP 400 表达的这一种空结果，没有被转换为共享循环原本就支持的空字符串。历史核查完成时尚未改处理逻辑，随后按用户授权实施了以下修复。

## 已实施的适配层修复

`src/tau2/voice/qwen_asr.py` 仅对 `HTTP 400 + code=CLIENT_ERROR + message=ASR_RESPONSE_HAVE_NO_WORDS` 返回 `QwenASRResult(text="")`，附带明确的 `empty_reason` 和安全的 Request ID。其他状态码、错误码、错误消息均继续走原来的异常路径，没有增加重试或吞掉任意 HTTP 400。

`QwenPlayedAudioTranscriber` 把这一结果的空识别状态、原因、Request ID 和音频片段时长保存到已有 usage 记录。音频继续通过既有录音保存；空结果不生成文字片段，后续片段继续由原来的识别循环处理。没有修改切片、模型、API Key、场景时钟或评分规则。

这是主机评测端的适配修改，下一次启动评测即可使用，不需要重建待测 Agent 镜像。

针对性的离线检查共 9 项通过，覆盖空识别后 worker 继续运行并正常识别下一段、空结果不产生文字、记录空识别证据、其他错误保持异常、原有错误正文脱敏及语音边界/热词行为。修改文件通过 Ruff 和格式检查；未执行整题回归或额外线上模型请求。

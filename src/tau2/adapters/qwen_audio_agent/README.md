# Qwen Audio Agent Adapter

本目录保存真实接入实现：

- `factory.py`：构造及公开场景超时／domain 绑定。
- `config.py`：镜像、环境变量引用、Gateway/MCP 和评测观察设置。
- `adapter.py`：tick、工具结果、媒体及中断。
- `provider.py`：Gateway 通信。
- `runtime.py`：单次试验容器与清理。
- `events.py`、`evidence.py`：原生后台事件和公共证据。
- `transcription/`：已播放音频的评测观察器。

## 部署与依赖

Dockerfile、构建脚本、Gateway bridge 和容器内观察器仍在
[integrations/qwen_audio_agent](../../../../integrations/qwen_audio_agent/README.md)。
先按部署说明构建可用镜像。Python runtime 保留现有隔离和清理机制。

配置只保存环境变量名称及配置文件路径。常见连接变量包括
QWEN_AUDIO_REALTIME_API_KEY、QWEN_AUDIO_REALTIME_URL、QWEN_AUDIO_REALTIME_MODEL、
QWEN_CODE_API_KEY、QWEN_CODE_API_BASE 和 QWEN_CODE_MODEL。
当前运行使用 Qwen 音频模型。共享配置中的 `qw.env` 选择原生 DashScope Qwen
Audio 前端，`realbench.env` 选择 Qwen 观察 ASR 与用户 TTS。使用
`integrations/qwen_audio_agent/config.gaia2.unified.json`；旧 deepseek 启动示例也
读取这一共享配置，不再启动历史 Speech-to-Speech 前端。不要在示例或结果里保存密钥。

## 新配置归属

推荐分别使用：

- `--qwen-agent-config`：被测系统连接、镜像、内部 speech_to_speech、观察 ASR。
- `--user-simulation-config`：模拟用户模型、TTS、check-in、动态追问和 watchdog。

第二份配置对应 `AudioNativeConfig.user_simulation`，同样可用于其他语音 Provider。
user_simulation_origin 记录显式配置或旧 Qwen 配置迁移的来源。

参见仓库中的：

```text
integrations/qwen_audio_agent/config.gaia2.adapter.example.json
examples/user-simulation.gaia2.json
```

最小参考命令：

```bash
tau2 run --audio-native --audio-native-provider qwen_agent \
  --benchmark none --domain gaia2 --language en --num-tasks 1 \
  --qwen-agent-config integrations/qwen_audio_agent/config.gaia2.adapter.example.json \
  --user-simulation-config examples/user-simulation.gaia2.json \
  --gaia2-allow-stale-runtime
```

最后一个参数仅用于整理期间的开发 smoke；正式运行沿用数据／代码版本封存流程。
实际运行会启动被测系统并调用配置的模型；上述命令不是验收步骤。

## 观察与能力

音频边界为：

- SAASR 最终输出和 adapter 外部输入：PCM16、16 kHz、单声道。
- Qwen bridge 输入：以 `voice.ready.inputSampleRate` 为准。当前原生
  `qwen-audio-3.1-realtime-flash` 握手返回 16 kHz，输入无需重采样；若声明
  24 kHz，仅在 adapter 内做 PCM 重采样。
- Agent 播放：PCM16、24 kHz，包含初始静音、播放队列及重连后的状态。
- Qwen 普通 HTTP ASR：直接接收已播放的 PCM24，按真实时长分段，再为 ASR
  API 重采样到 16 kHz；Qwen 流式 ASR 直接读取实际 playback 采样率。

这条链路不经过 8 kHz 降采样或 μ-law 编解码。只把 16 kHz 输入的元数据改为
24 kHz 会改变时长并产生错误音频；必要转换必须重采样实际样本。
转写只能观察实际播放给用户的音频，不使用未播放 caption 代替听到的回复。

热词材料由评测侧选择，adapter 只接收已准备的 profile。它不能读取完整题目、
未来阶段、隐藏值或参考答案。观察 ASR 与被测系统自己的 ASR/TTS 属于不同配置。

后台证据通过 get_async_evidence_source() 返回能力、状态和事件。诊断与元信息在
关闭后仍可读取；缺少后台观察或完整费用账本时明确保留不可用状态。

## 旧输入兼容

旧 voice/audio_native/qwen_agent 导入及 persistent_tasks/adapters/qwen 导入转发到同一份
新实现。新内部代码使用本目录。

旧 Qwen JSON 顶层的 tts、user_decision_llm、user_llm_*、user_silence_checkin_seconds、
dynamic_followup 和 watchdog 在加载时归入评测配置。独立读取旧 Qwen JSON 时，
evaluation 仅作兼容载体；AudioNativeConfig 将其迁入 user_simulation 后清空，不在
运行时重复解析。新旧显式值冲突会报错，不自动选择其一。

speech_to_speech 中的 TTS 是被测系统内部设置，始终留在系统配置。
容器、队列和转写状态属于本次试验，不跨试验共享。

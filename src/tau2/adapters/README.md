# 待测 Agent 适配入口

本目录集中保存待测系统的接入实现，当前已迁移 Qwen Audio Agent。
不完整的 GPT Live（`openai_live`）和 Gemini Live（`gemini`）已移除，后续按本目录规范重新接入。
普通 OpenAI Realtime（`openai`）及其他现有 Provider 保留。

## 选择方式

现有 CLI 标识保持不变：

```bash
tau2 run --audio-native --audio-native-provider qwen_agent \
  --benchmark none --domain gaia2 \
  --qwen-agent-config integrations/qwen_audio_agent/config.gaia2.adapter.example.json \
  --user-simulation-config examples/user-simulation.gaia2.json
```

运行前按 [Qwen 说明](qwen_audio_agent/README.md) 配置环境变量及部署镜像。
命令仅用于说明，不会在导入适配器时执行。

## 接入结构

`registry.py` 保存延迟 factory 模块映射，只创建本次试验实例，不缓存连接。
现有 `voice/audio_native/adapter.py:create_adapter()` 将 Qwen 构造转交此入口；
其他 Provider 保持现有路径。新 factory 不应反调旧 factory。

适配器沿用 `DiscreteTimeAdapter` 的连接、tick、工具结果、中断和关闭协议。
公共音频转换、播放缓冲、TickResult 与后台事件类型保持复用。

可选公开接口：

- `enable_diagnostics()`：在连接前启用被动记录。
- `get_diagnostic_evidence()`：返回实际后台输入和观察证据。
- `get_async_evidence_source()`：按能力提供后台事件，不执行任务。
- `get_run_metadata()`：返回公开元信息，未知费用保留不可用。
- `recheck_heard_audio()`：可选的已播放音频观察复查。

静态 `AdapterBehavior` 描述提示词、初始消息、关闭前工具结果和播放格式。
读取这些设置不创建网络连接。未实现的证据接口返回不适用／不可用，不记 Agent 零分。

## 新增适配

1. 建立对应系统目录，提供 config、factory、adapter 和 README。
2. 实现已有通信协议，复用公共媒体和消息类型。
3. 在注册入口登记 factory 模块；可选提供观察准备描述。
4. 如系统需要部署，将镜像和外部材料放在 integrations，运行客户端放在适配目录。
5. 用本地假 Provider 验证连接边界、工具交付和清理。

用户策略、任务选择、隐藏信息和评分留在评测侧。适配器只接收所需公开运行参数。

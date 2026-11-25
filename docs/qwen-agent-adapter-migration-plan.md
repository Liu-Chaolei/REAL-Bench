# Qwen Audio Agent 统一 Adapter 层迁移计划

状态：已实施。Qwen 实现已迁入统一 Adapter 层，旧导入保留兼容；用户配置和公开证据接口已接通。

## 1. 目标与范围

将 Qwen Audio Agent 的实际接入实现集中到：

```text
src/tau2/adapters/qwen_audio_agent/
```

通用 Agent 和 Runner 通过明确接口调用适配器；模拟用户、场景控制和评分配置由评测侧管理。

第一轮优先完成 Qwen 的整理，为 GPT Live、Gemini Live 等待测 Agent 留出一致的接入方式，但不同时迁移所有 Provider。

本轮以迁移和解耦为主：

- 复用现有音频、工具、事件和资源清理机制。
- 不重写 Qwen 协议和被测系统内部逻辑。
- 不调整任务、用户策略或评分含义。
- 不新增通用调度框架、运行时审计或自动修复系统。
- 不覆盖其他已有工作区改动。

## 2. 目标目录

```text
src/tau2/adapters/
├── __init__.py
├── README.md
├── registry.py
└── qwen_audio_agent/
    ├── __init__.py
    ├── README.md
    ├── factory.py
    ├── config.py
    ├── adapter.py
    ├── provider.py
    ├── runtime.py
    ├── evidence.py
    ├── events.py
    └── transcription/
        ├── __init__.py
        ├── base.py
        ├── played_audio.py
        ├── qwen.py
        ├── qwen_streaming.py
        └── mimo.py
```

按实际复杂度拆文件，不要求后续每种 Agent 都实现相同数量的模块。

| 模块 | 职责 |
| --- | --- |
| factory.py | 根据已解析配置构造 Qwen 适配器 |
| config.py | Qwen 系统连接、启动、协议和专属观察设置 |
| adapter.py | 对接现有 tick、音频、工具结果与中断接口 |
| provider.py | Gateway 通信、协议事件收发 |
| runtime.py | 本次试验的容器启动、关闭和资源清理 |
| evidence.py | 诊断证据、后台证据和运行元信息 |
| events.py | Qwen 原生事件到公共事件的转换 |
| transcription/ | 评测侧实际播放音频的观察转写 |

公共音频转换、播放缓冲、TickResult 和后台事件模型继续复用现有模块。暂不为了目录完整把公共代码复制到新目录。

包初始化保持轻量：导入时不读取凭据、不创建连接、不启动线程或容器，也不强制加载所有可选依赖。

## 3. 现有模块迁移

| 当前位置 | 目标 |
| --- | --- |
| voice/audio_native/qwen_agent/discrete_time_adapter.py | adapters/qwen_audio_agent/adapter.py |
| voice/audio_native/qwen_agent/provider.py | adapters/qwen_audio_agent/provider.py |
| voice/audio_native/qwen_agent/runtime.py | adapters/qwen_audio_agent/runtime.py |
| voice/audio_native/qwen_agent/config.py | 拆分后进入新 config.py 和评测侧配置 |
| voice/audio_native/qwen_agent/async_evidence.py | adapters/qwen_audio_agent/evidence.py |
| Qwen 专属播放转写模块 | adapters/qwen_audio_agent/transcription/ |
| persistent_tasks/adapters/qwen.py | adapters/qwen_audio_agent/events.py |

### 3.1 单一实现来源

- 新目录持有真实实现。
- 仓库内部调用切换到新路径。
- 旧路径暂留明确的兼容导出，不复制逻辑。
- 类名可以先保留，例如 QwenAgentConfig 和 DiscreteTimeQwenAgentAdapter。
- 更新内部模块导入及相对资源定位，特别是通过 __file__ 和 parents[] 查找文件的逻辑。
- 配置标识 qwen_agent 和 CLI 参数 --qwen-agent-config 暂时保留。
- 历史结果中的模块、配置和 Provider 标识不批量改写。

### 3.2 公共能力边界

公共事件类型继续位于公共模型层。Qwen 专属的原生事件字段映射进入 events.py。

音频缓冲、tick 时序、共享播放记录等能力沿用现有 DiscreteTimeAdapter 基础设施，不在本轮设计另一套流式协议。

## 4. 统一创建入口与依赖方向

在 adapters/registry.py 登记 Qwen factory，使用延迟导入。

第一轮调用关系：

```text
Runner
  → 通用音频 Agent
    → 现有 create_adapter()
      → 新 adapters 注册入口
        → Qwen factory
          → Qwen adapter
```

其他 Provider 暂时继续走现有 create_adapter() 分支。

新注册入口只构造已迁移的适配器，不反过来调用旧 create_adapter()，避免循环调用。

现有 tau2.registry 继续管理 Agent 类型和领域注册。Adapter 注册入口只管理传输／系统接入实现，不替代整个框架注册表。

### 4.1 Factory 输入

输入限制为适配器实际需要的内容：

- Qwen 专属配置。
- tick 时长和输入音频格式。
- 模型和语言。
- 日志目录。
- 已准备好的观察器配置。
- 必要公开运行上下文，例如 domain 和工具等待上限。

不传入完整 RunConfig、完整任务对象、参考答案、隐藏信息或评分器。

Factory 只解析和构造本次实例。实际网络连接与容器启动由现有连接生命周期执行。

## 5. 收拢通用 Agent 中的 Qwen 分支

当前 DiscreteTimeAudioNativeAgent 中的以下行为应由适配器接口提供：

| 当前 Qwen 特殊处理 | 目标方式 |
| --- | --- |
| 只传业务 policy，保留远端原生提示词 | 适配器提供提示词传递策略 |
| 初始 tick 不伪造欢迎语 | 适配器声明初始消息行为 |
| 特殊播放音频格式 | 从适配器取得输出格式 |
| 结束时补交待发送工具结果 | 适配器声明并实现关闭前结果处理 |
| 读取 Qwen 后台诊断数据 | 调用公开诊断证据接口 |
| 判断待播放输出 | 继续复用 has_pending_output |

优先复用现有接口，只增加当前确实缺少的方法或属性。例如：

```python
get_diagnostic_evidence()
get_async_evidence_source()
get_run_metadata()
enable_diagnostics()
playback_audio_format
```

提示词、欢迎语和关闭行为使用少量明确策略表达，不引入大量任意字符串开关。

要求：

- 接口默认值保持其他 Provider 的现有行为。
- 读取静态格式或策略不能建立网络连接。
- 补交工具结果不能额外触发一次 Agent 回答。
- 保留实际播放与已接收音频的区别，不用尚未播放的文字充当用户已经听到的回复。
- 不改变现有 Agent 是否拥有 adapter 资源的规则，避免关闭调用方管理的连接。

## 6. 收拢 Runner 的内部对象访问

Runner 不再直接访问：

```python
agent.adapter.provider.diagnostics_enabled
adapter.provider.metadata
adapter.config.asr_model
adapter.transcriber.summary()
```

目标数据流：

```text
Runner 请求启用诊断 → Adapter 配置内部观察器
Runner 请求运行元信息 → Adapter 返回可保存的公开信息
诊断模块请求证据 → Adapter 返回公共证据结构
```

具体调整：

- 通过 enable_diagnostics() 启用观察。
- 通过 get_run_metadata() 取得系统元信息、观察 ASR 模型和结束延迟。
- 通过 get_diagnostic_evidence() 取得后台输入及状态观察。
- 通过 get_async_evidence_source() 取得后台事件接口。
- 音频复查通过公开接口接入，Runner 不再读取 agent._adapter 后寻找内部方法。
- Qwen MCP 超时与环境等待时间的适配、domain 注入集中到 Qwen factory，输入只含必要公开参数。

证据接口只返回实际观察到的事实，不替 Agent 执行任务，不伪造内部计划。

没有完整费用账本时继续表达不可用，不能填零。缺少诊断能力时保留不适用／不可用状态，不把它当作 Agent 能力零分。

## 7. 配置按归属拆分

不能把旧 QwenAgentConfig 整体搬迁后继续让它同时管理双方行为。

### 7.1 被测系统配置

归 Qwen Adapter：

- 镜像、连接地址和启动参数。
- Gateway 和 MCP 协议参数。
- Qwen 系统内部模型与语音前端。
- 工具请求超时。
- 容器生命周期设置。

需要特别区分被测系统内部 TTS 与模拟用户 TTS，不能因它们位于同一旧文件就一起迁移。

### 7.2 模拟用户和场景配置

归评测侧：

- 模拟用户 LLM、决策模型及参数。
- 模拟用户 TTS。
- 用户静默 check-in。
- 动态追问。
- watchdog 等运行控制。

动态追问由运行／用户配置决定。切换到其他待测 Agent 时，不应再从 qwen_agent_config 查找这套策略。

配置解耦不代表所有 Agent 自动获得支持。如果机制需要特定证据，应根据证据能力决定是否可用，不再以 Provider 名称作为唯一判断。

### 7.3 评测观察器配置

实际播放音频 ASR、热词和音频复查属于评测观察能力。

Qwen 专属观察器实现可位于 Adapter 目录，但配置和记录必须区分：

- 被测 Agent 内部语音能力。
- 评测框架为了观察实际输出而执行的转写。

热词材料选择由评测侧完成。Adapter 接收已经准备好的材料，不自行读取未来阶段、隐藏值或参考答案。

现有词汇选择规则本轮不重新定义；迁移不能扩大适配器的信息可见范围。

## 8. 旧配置在入口转换一次

保留有限的旧 Qwen JSON 转换入口：

1. 读取旧配置。
2. 将连接和启动设置送入 Adapter 配置。
3. 将用户和场景设置送入评测配置。
4. 在运行前形成最终生效配置。
5. 后续模块只读取各自的新配置对象。

规则：

- 新旧字段同时存在且冲突时明确报错。
- 只提供旧字段时保留原有行为。
- 默认值继续从中央配置读取，避免新旧模块各定义一份。
- 结果记录最终生效配置及必要配置来源，不记录密钥。
- 不改写历史结果。
- 每次配置加载只转换一次，不在 tick 热路径重复处理。
- 不增加自动猜测、自动修复旧配置或通用迁移框架。

## 9. 部署目录与资源生命周期

integrations/qwen_audio_agent/ 继续保存：

- Dockerfile 和镜像构建脚本。
- bridge、Gateway 包装与容器内观察器。
- 外部系统部署和启动材料。
- 部署说明及相关资源。

Python 侧启动客户端迁入 Adapter 的 runtime.py，调用部署产物并管理本次试验资源。

既有容器隔离、超时清理、密钥脱敏机制原样迁移，不在本轮放宽或重写。

每个试验创建独立的 adapter、provider 和 runtime。注册表只登记 factory，不缓存连接实例，不让并发试验共享容器、事件队列或转写状态。

## 10. 使用说明与示例

### 10.1 adapters/README.md

说明：

- 已接入的待测 Agent。
- 如何选择适配器。
- 公共输入输出、工具结果及证据接口。
- 如何增加新的 Agent 适配目录。
- 第一轮仅迁移 Qwen，其他 Provider 的当前入口。

### 10.2 qwen_audio_agent/README.md

说明：

- 最小运行配置和参考命令。
- 依赖及环境变量名称。
- 部署材料位置。
- 支持的输入输出模式。
- 实际可提供的证据。
- 被测系统、模拟用户和评测观察器配置如何区分。
- 旧导入及旧配置的兼容规则。

更新示例配置，使使用者无需阅读 Runner 源码即可理解各项设置归属。

## 11. 实施顺序

### 单元一：迁移真实模块

- 建立新目录。
- 迁移 Qwen 适配、Provider、容器、事件和转写实现。
- 更新内部导入与资源定位。
- 保留旧路径兼容导出。
- 更新运行时指纹涉及的代码路径。

### 单元二：统一创建与生命周期

- 登记 Qwen factory。
- 现有 create_adapter() 转交新入口。
- 收拢通用 Agent 中的 Qwen 特殊行为。
- 保留音频播放、工具返回、中断和结束清理语义。

### 单元三：Runner 与配置解耦

- Runner 改用公开诊断、复查和元信息接口。
- 模拟用户、动态追问和运行控制配置移回评测侧。
- 配置加载入口完成一次旧字段转换。
- 去除对应的 Runner 内部对象访问和重复配置解析。

### 单元四：文档和回归

- 更新最小示例、使用说明和整理文档。
- 调整现有假 Provider 测试导入。
- 补充少量接口边界测试。
- 完成残留调用核对和改动文件静态检查。

不为通过版本检查直接改写冻结数据的 pin。运行时指纹变化需要如实报告；正式数据封存沿用独立流程。

## 12. 轻量验证范围

优先复用已有 Qwen 假 Provider 测试，必要时补充：

1. 新入口能构造 Qwen Adapter，旧导入指向同一实现。
2. 导入 adapters 或选择其他 Agent 不启动 Qwen 资源，不强制加载其可选依赖。
3. 输入音频、实际播放格式和转写归属保持正确。
4. 工具结果正常返回，结束时待交付结果不丢失、不额外触发回答。
5. 中断、关闭和重复清理保持现有行为。
6. 诊断、后台证据和运行元信息通过公开接口取得。
7. 旧配置转换保留原值，新旧冲突明确报错。
8. 用户策略不再依赖 Qwen 专属配置。
9. 独立试验不共享适配器实例。

只运行相关本地测试和改动文件的 Ruff。

不要求：

- 启动真实容器或调用真实模型。
- 运行完整题集。
- 对每个 Provider 做全量端到端验收。
- 扫描历史结果或自动迁移全部配置。
- 在正式运行中增加依赖扫描、一致性巡检、额外裁判或自动修复。

运行时正确性主要由明确依赖方向、配置归属、公开接口和每试验独立实例保证。

## 13. 完成标准

- Qwen 的实际接入实现集中在 adapters/qwen_audio_agent/。
- 旧路径仅作兼容导出，真实实现只有一份。
- 通用 Agent 不再承担本轮识别出的 Qwen 专属提示词、欢迎语、格式和诊断处理。
- Runner 不再直接读取 Qwen Provider 或转写器内部属性。
- 用户策略、动态追问和运行控制配置归评测侧。
- 适配器只接收必要公开运行信息及已准备的观察材料。
- 其他 Provider 沿用当前行为，可以在后续按相同方式迁移。
- 部署材料与评测侧接入客户端职责清楚。
- 现有配置标识及历史结果保持可解释。
- 相关本地测试通过，正式评测中没有新增重型验收或自动修复负担。

## 14. 实施记录

- 实际实现迁入 `src/tau2/adapters/qwen_audio_agent/`；旧 Python 模块仅转发到同一模块对象。
- 延迟 registry／factory、AdapterBehavior 和公开诊断／元信息／复查接口已接通。
- 模拟用户、TTS、check-in、动态追问和 watchdog 使用
  `AudioNativeConfig.user_simulation`；CLI 新增 `--user-simulation-config`。
- 旧顶层用户字段先进入兼容 evaluation 载体，在 AudioNativeConfig 加载时迁出并清空。
  配置来源记录于 user_simulation_origin，显式冲突会拒绝；规范化配置可重复读取。
- 观察热词只返回公开准备需求，材料选择继续由评测侧承担；Runner 不读取 Qwen Provider 内部属性。
- integrations 继续保留部署代码，新系统与用户配置示例及两个层级 README 已添加。
- 运行时指纹包含新真实实现和配置模块，未手工修改冻结任务或 pin。
- 本地相关回归：133 项通过，1 项因本机缺少 libasound.so.2 排除。
  测试使用假 Provider、临时配置及合成凭据，没有启动真实容器或调用真实模型。

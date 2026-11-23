# API 配置统一管理实施计划

状态：已实施。本文保留原实施方案；当前字段、使用方法与迁移选择见 [API 配置使用说明](api-configuration.md)。

## 1. 目标与范围

将供应商连接、被测 Agent 的连接选择、评测侧的连接选择分别集中到三份文件：

| 文件 | 职责 |
| --- | --- |
| `/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/config.env` | 可用供应商的 Key、Base URL、模型 |
| `/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/qw.env` | Qwen Audio Agent 前端、后台的连接选择及调用参数 |
| `/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio/realbench.env` | 用户模拟器、用户侧语音、评分、请求生成、出题和审题的连接选择 |

本次覆盖当前 Gaia2 + Qwen Audio Agent 完整运行链路，以及相关评分和生成脚本，不扩展到仓库所有可选领域与供应商。

目标行为：换模型或换 Key 时，修改对应 env 文件即可生效；不再需要同步修改多个 JSON、YAML 和 Python 常量。配置在启动时解析，不增加逐轮配置审计、在线验收、额外裁判或模型探测。

## 2. 当前配置分散情况

代码盘点时发现：

- Agent 前后台、用户模型已有环境变量引用能力，但运行 JSON 可能直接引用 `DEEPSEEK_*`，绕过 `GAIA2_BACKEND_*`。
- 用户侧 ASR/TTS 的模型与地址仍主要写在 Agent JSON 中。
- 评分 YAML 中存在写死的模型、地址和 API 协议。
- 独立 Intent 入口使用 `INTENT_GT_*`、`INTENT_PREDICTION_*`、`INTENT_EVALUATION_*`，与整通话诊断使用的模型配置不是同一个入口。
- `smoke.py` 通过全局 `OPENAI_*` 配置部分裁判；`--judge-from-user-llm` 还会改写 Agent 后台映射，需要解除这一耦合。
- 主动请求生成、离线出题及审阅分别有自己的模型引用逻辑。
- 当前共用 env 加载器只读取一个文件，尚不自动组合三文件配置。
- 盘点时 `qw.env` 仍包含重复供应商配置和评分配置，`realbench.env` 尚未创建。实施前应重新检查实际文件，避免覆盖用户后续修改。

当前主要 API 调用包括：

| 调用类别 | 用途 |
| --- | --- |
| Agent 语音前端 | 接收用户语音、生成 Agent 语音回复 |
| Agent 后台 | 推理、工具调用、后台任务执行 |
| 用户模拟器 | 回复生成、打断/附和判断、动态追问及其核对 |
| 用户侧 ASR/TTS | 识别 Agent 实际播放语音、生成模拟用户语音 |
| 可选用户分支 | 回复事实核对、热词提取、隐藏信息提问识别 |
| 诊断评分 | 需求理解、执行质量、发言有用性、状态交互、精简反馈 |
| 后台请求 | 请求措辞生成、回复语义判断 |
| 离线生成 | 出题、审阅、改写、修复及相关重评分 |

纯语音时序测量、环境状态检查、分数汇总本身不调用模型 API。当前 Gaia2 已取消的独立 NL_ASSERTION 与整题 COMMUNICATE 裁判不应借此次迁移恢复。

## 3. 供应商连接池：config.env

保留用户现有供应商命名：`QWEN_AUDIO`、`GPT`、`MINIMAX1`、`MINIMAX2`、`MIMO`、`DEEPSEEK`、`QWEN`。

示意结构，省略号不是可执行配置值：

```dotenv
GPT_API_KEY=...
GPT_BASE_URL=...
GPT_MODEL=...

MINIMAX1_API_KEY=...
MINIMAX1_BASE_URL=...
MINIMAX2_API_KEY=...
MINIMAX2_BASE_URL=...
MINIMAX_MODEL=...

MIMO_API_KEY=...
MIMO_BASE_URL=...
MIMO_MODEL=...
MIMO_MODEL2=...

DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=...
DEEPSEEK_MODEL=...
```

规则：

- 真实 Key 只保存在连接池中，角色文件使用 `${...}` 引用。
- 一个供应商可以有多个模型变量，不强制限制为单个 `MODEL`。
- MINIMAX1、MINIMAX2 若提供同一模型，可以共用 `MINIMAX_MODEL`。
- 语音服务可以为 Realtime、ASR、TTS 分别定义地址；共用 Key 不意味着各接口地址相同。
- 不将整个连接池注入 Agent 容器。

## 4. Agent 配置：qw.env

优先沿用 Agent 已有变量名称。以下是选择 DeepSeek 后台的结构示例，迁移时应保留用户实际选定的模型：

```dotenv
QWEN_AUDIO_REALTIME_PROVIDER=${QWEN_AUDIO_PROVIDER}
QWEN_AUDIO_REALTIME_MODEL=${QWEN_AUDIO_MODEL}
QWEN_AUDIO_REALTIME_BASE_URL=${QWEN_AUDIO_BASE_URL}
DASHSCOPE_API_KEY=${QWEN_AUDIO_API_KEY}

GAIA2_BACKEND_MODEL=${DEEPSEEK_MODEL}
GAIA2_BACKEND_API_BASE=${DEEPSEEK_BASE_URL}
GAIA2_BACKEND_API_KEY=${DEEPSEEK_API_KEY}
GAIA2_BACKEND_REASONING_EFFORT=low

QWEN_AUDIO_AGENT_BACKEND_MODEL=openai/${GAIA2_BACKEND_MODEL}
```

`OPENCODE_CONFIG_CONTENT` 保留原有 JSON 结构，内部继续引用角色变量：

```dotenv
OPENCODE_CONFIG_CONTENT='{
  "provider": {
    "openai": {
      "npm": "@ai-sdk/openai",
      "options": {
        "baseURL": "${GAIA2_BACKEND_API_BASE}",
        "apiKey": "${GAIA2_BACKEND_API_KEY}"
      }
    }
  }
}'
```

### 4.1 当前评测后台的实际消费方式

当前 `integrations/qwen_audio_agent/bridge.mjs` 使用 Qwen Code 后端，读取 `OPENAI_MODEL` 等变量，并设置 `AGENT_PROTOCOL=qwen`。它目前不通过 `OPENCODE_CONFIG_CONTENT` 配置后台。

因此需要在启动适配层完成映射：

| qw.env 角色变量 | 当前 Qwen Code 接收变量 |
| --- | --- |
| `GAIA2_BACKEND_MODEL` | `OPENAI_MODEL` |
| `GAIA2_BACKEND_API_BASE` | `OPENAI_BASE_URL` |
| `GAIA2_BACKEND_API_KEY` | `OPENAI_API_KEY` |
| `GAIA2_BACKEND_REASONING_EFFORT` | `QWEN_CODE_REASONING_EFFORT` |

同一组后台选择供 OpenCode 配置引用，也供当前评测适配器使用。切换后台实现不属于本次配置迁移。

## 5. 评测配置：realbench.env

统一采用 `REALBENCH_` 前缀，避免与 Agent 的变量混淆。文本模型角色使用：

```dotenv
REALBENCH_USER_MODEL=${DEEPSEEK_MODEL}
REALBENCH_USER_BASE_URL=${DEEPSEEK_BASE_URL}
REALBENCH_USER_API_KEY=${DEEPSEEK_API_KEY}
REALBENCH_USER_API=chat
```

可选调用参数包括：

```dotenv
REALBENCH_USER_REASONING_EFFORT=low
REALBENCH_USER_TEMPERATURE=0
```

迁移时保持原调用参数含义，不自动为所有模型添加 temperature 或 reasoning effort。各项功能是否开启仍由实验配置决定。

### 5.1 角色与默认继承

下表为拟采用的角色命名，尚未在代码中实现：

| 前缀（均以 REALBENCH_ 开头） | 调用用途 | 默认继承 |
| --- | --- | --- |
| `USER` | 用户回复生成 | 独立配置 |
| `USER_DECISION` | 打断、附和判断 | USER |
| `USER_GROUNDING` | 启用时的用户回复事实核对 | USER |
| `FOLLOWUP` | 动态追问生成与核对 | USER |
| `HIDDEN_INTENT` | 启用时的隐藏信息提问识别 | USER |
| `ASR_HOTWORDS` | 启用时的热词提取 | USER |
| `JUDGE` | 诊断评分默认模型 | 独立配置 |
| `INTENT_GT` | 用户需求提取 | JUDGE |
| `INTENT_PREDICTION` | Agent 理解提取 | JUDGE |
| `INTENT_EVALUATION` | 需求匹配判断 | JUDGE |
| `EXECUTION` | 执行质量 | JUDGE |
| `INTERACTION` | 发言有用性 | JUDGE |
| `STATE_INTERACTION` | 状态交互语义判断 | JUDGE |
| `FEEDBACK_NUGGETS` | 精简反馈信息点提取 | JUDGE |
| `FEEDBACK_ALIGNMENT` | 精简反馈信息匹配 | JUDGE |
| `REPLY_JUDGE` | 后台请求回复判断 | JUDGE |
| `ACTIVE_REQUEST` | 主动后台请求措辞生成 | USER |
| `AUTHOR` | 离线出题、改写、修复 | 独立配置 |
| `REVIEWER` | 离线审题 | 独立配置 |

通常只需要填写 USER、JUDGE、AUTHOR、REVIEWER，再配置有特殊需求的角色。

例如，评分默认使用 DeepSeek，执行质量单独使用 GPT：

```dotenv
REALBENCH_JUDGE_MODEL=${DEEPSEEK_MODEL}
REALBENCH_JUDGE_BASE_URL=${DEEPSEEK_BASE_URL}
REALBENCH_JUDGE_API_KEY=${DEEPSEEK_API_KEY}
REALBENCH_JUDGE_API=chat

REALBENCH_EXECUTION_MODEL=${GPT_MODEL}
REALBENCH_EXECUTION_BASE_URL=${GPT_BASE_URL}
REALBENCH_EXECUTION_API_KEY=${GPT_API_KEY}
REALBENCH_EXECUTION_API=responses
```

继承规则固定在代码中：

1. 未配置角色时，继承表中的默认角色。
2. 更换连接时，模型、地址、Key 三项必须完整提供，防止新模型误用旧连接的凭据；协议也应显式配置以避免沿用不适用的调用方式。
3. 同一连接仅修改 reasoning effort 等参数时，可以单独覆盖参数。
4. 只解析本次启用功能需要的角色，不要求未使用供应商配置完整。
5. 缺失必需连接时报错，不静默退回 Python 中的另一个模型。

### 5.2 用户侧 ASR/TTS

语音接口单独配置，不复用文本模型的 Chat/Responses 协议字段：

```dotenv
REALBENCH_ASR_PROVIDER=qwen
REALBENCH_ASR_MODEL=qwen-audio-3.1-asr-flash
REALBENCH_ASR_BASE_URL=${QWEN_AUDIO_BASE_URL}
REALBENCH_ASR_API_KEY=${QWEN_AUDIO_API_KEY}

REALBENCH_TTS_PROVIDER=qwen
REALBENCH_TTS_MODEL=qwen-audio-3.1-tts-flash
REALBENCH_TTS_BASE_URL=https://dashscope.aliyuncs.com
REALBENCH_TTS_API_KEY=${QWEN_AUDIO_API_KEY}
REALBENCH_TTS_VOICE=loongbella_v3.1
REALBENCH_TTS_VOICES_JSON='{"mamadou_diallo":"Brian_v3.1","priya_patil":"loongbella_v3.1"}'
```

上例的 ASR 地址引用依赖现有 Qwen ASR 适配器支持的地址转换。Realtime WebSocket 与 TTS HTTP 地址分别管理。若要把所有供应商地址均归入连接池，可增加 `QWEN_AUDIO_ASR_BASE_URL`、`QWEN_AUDIO_TTS_BASE_URL`，由此引用。

声线属于供应商相关设置，放入 realbench.env；采样、热词开关、时序和音效等实验设置继续留在现有运行配置。

## 6. 共用加载与解析机制

### 6.1 加载路径

```text
Agent：config.env + qw.env → Agent 角色配置
评测： config.env + realbench.env → 评测与生成角色配置
```

在现有 `src/tau2/utils/env_config.py` 上增加多文件读取能力，先保留原始引用，合并后再递归解析所需变量。

- 默认使用用户配置目录下的 qwaudio；本机使用本文开头指定的目录。
- 各入口统一支持配置目录覆盖，例如 `--api-config-dir`。
- 保留必要的单文件路径覆盖能力，兼容现有 `config_env_file`。
- Agent 与评测拥有各自配置视图，评测不继承 qw.env 的后台选择。
- 同一文件重复定义变量时报告变量名与行号。
- 新模式下供应商变量归 config.env 所有，角色文件不重复定义它们。
- 缺失引用、空必填值、循环引用在启动解析时报告。
- 普通字符串和多行 JSON 中的 `${...}` 使用相同递归解析规则。
- 不通过 shell source 执行文件，不依赖文件中变量出现的先后顺序。
- 展开 OPENCODE_CONFIG_CONTENT 后解析 JSON；重新输出 JSON 时使用序列化器，避免手工拼接。

### 6.2 优先级与兼容性

新模式采用配置文件作为日常配置来源，明确传入的调用参数可以作为最高优先级覆盖。shell 中残留的 OPENAI_* 不得悄悄改变新角色配置。

旧单文件接口保留原有进程环境优先规则，避免改变与此次迁移无关的调用方。新旧模式应由明确的入口设置区分；同一运行出现相互冲突的连接来源时报告冲突位置，不依赖隐式优先级猜测。

### 6.3 角色解析模块

建议新增 `src/tau2/utils/api_config.py`，负责：

1. 加载连接池与角色文件。
2. 按固定角色表解析选择、继承和参数覆盖。
3. 返回文本或语音配置对象。
4. 转换为 LiteLLM 参数、语音客户端参数或 Agent 子进程变量。

使用小型 Pydantic 模型表达配置。凭据不进入普通打印或结果序列化，仅在构造请求时取出。

文本协议统一转换：

```text
API=chat      → openai/<model>
API=responses → openai/responses/<model>
```

env 保存供应商原始模型名，旧配置已有前缀的模型在兼容入口规范化一次。当前适配器不支持的协议明确报错。此模块不改变调用、重试、预算和评分逻辑。

## 7. Agent 与用户模拟器接入

主要涉及：

- `src/tau2/voice/audio_native/qwen_agent/config.py`
- `src/tau2/voice/audio_native/qwen_agent/runtime.py`
- `integrations/qwen_audio_agent/smoke.py`
- `src/tau2/runner/build.py`
- 用户侧 ASR/TTS 配置构造位置及需要区分角色的用户调用点。

实施内容：

1. QwenAgentConfig 的 Agent 连接从 qw.env 解析。
2. 用户模型、用户侧 ASR/TTS 从 realbench.env 解析。
3. smoke.py 保留实验参数与启动职责，不再以全局 OPENAI_* 同时控制多个角色。
4. builder 将解析后的角色配置传给回复、判断、动态追问等现有调用点。
5. 一个运行进程使用同一份启动配置快照，不在每轮对话中重新读文件。
6. Agent 容器只注入其实际需要的前后台设置，不注入评分凭据和完整连接池。
7. `--judge-from-user-llm` 若保留为兼容选项，只令裁判复用用户模型，删除其改写 Agent 后台的行为。
8. 当前运行 JSON 移除写死模型、地址和供应商映射，继续保存镜像、超时、时序和功能开关。

## 8. 评分接入

复用 `src/tau2/diagnostics/config.py` 已有 ModelProfile 结构，结合统一角色解析：

- YAML 保存 measures、并发、请求预算、calibration 等实验配置。
- 模型连接从 realbench.env 对应角色解析。
- TauJsonModel 接收已解析配置，不再依赖全局环境中恰好存在某个 Key。
- 实时自动评分与 `tau2 metrics run` 共用同一配置加载逻辑。
- 独立 Intent 入口映射到 REALBENCH_INTENT_*；旧 INTENT_* 仅作为明确的兼容入口。
- 后台回复裁判使用 REALBENCH_REPLY_JUDGE，不恢复已经取消的整题裁判。
- 新模式显式选择 realbench.env 后，历史轨迹中的 metrics_config 不得覆盖当前模型选择。

## 9. 主动请求与离线生成接入

主动请求使用 REALBENCH_ACTIVE_REQUEST，移除 active-metrics YAML 内的供应商绑定，以及缺少变量时退回写死模型的行为。原有超时和模板回退机制保留。

离线生成映射：

```text
author   → REALBENCH_AUTHOR
reviewer → REALBENCH_REVIEWER
```

生成 JSON 保留并发、种子、上下文长度、尝试次数等参数。

更新相关生成、修复、审题、重评分脚本，使其使用相同配置目录入口，包括 `scripts/rescore_gaia2_outcomes.py` 等仍直接读取旧变量的脚本。

现有 MiniMax 按模型名称自动选择第二地址的机制改成显式备用连接：只在 realbench.env 配置备用连接时启用，不再依赖旧 MINIMAX_API_KEY2 命名或模型名猜测。备用连接的具体字段随角色解析模块一起定义，必须提供完整连接。已有 Key 池继续使用连接池变量引用，调度参数保留在生成配置中。

## 10. 运行记录、缓存与凭据边界

运行结果记录每个实际使用角色的：

- 模型、协议、非敏感 Base URL。
- 实际生效且影响输出的模型参数。
- 角色、配置来源、Key 引用名称。
- 是否继承默认角色。

真实 Key、展开后的完整 OPENCODE_CONFIG_CONTENT 不写入结果、日志或命令行参数。端点如含认证信息，应在记录时去除认证部分。

诊断缓存与生成续跑标识使用实际模型、地址、协议及影响输出的参数，不使用真实 Key：

- 换模型、地址或协议后不误用旧缓存。
- 仅轮换 Key 不导致全部缓存失效。
- 修改 env 文件影响下一次启动，已启动运行继续使用既有快照。
- 历史报告保留历史模型记录，主动重评分记录新配置。

## 11. 迁移顺序

### 第一步：实现基础模块

增加多文件加载和角色解析能力，先保持现有入口行为。明确新旧模式边界。

### 第二步：整理私有文件

实施前重新读取当前文件。为现有文件保存权限受限的备份，将 qw.env 的重复供应商设置改为引用，将评分设置迁移到新建 realbench.env，保持私有文件权限为 0600。

分别记录 qw.env 当前选择与最近评测实际选择，两者可能不同，不为消除差异而擅自换模型。本文示例模型仅说明结构。

### 第三步：接入 Agent、用户与语音

打通启动快照、子进程映射、用户与语音角色配置，解除 judge-from-user 对后台的额外影响。

### 第四步：接入评分与生成

统一在线/离线评分、主动请求、出题和审题的解析方式，处理协议、备用连接和历史配置回退。

### 第五步：迁移 JSON/YAML

更新当前使用的配置，移除重复连接设置；旧配置保留明确兼容入口。不要重写历史结果、固定题目数据或已有实验记录。

### 第六步：文档与示例

仓库只保存不含真实凭据的示例，说明变量、继承与换模型方法。私有 env 不进入仓库。

文档至少包含：更换模拟用户模型、更换统一裁判、单独更换一个评分角色、切换 MiniMax 线路、修改 ASR/TTS、运行独立评分和生成脚本。

## 12. 轻量验证

验证聚焦配置机制，使用本地假客户端或 mock：

1. 跨文件、多层引用、多行 JSON 正确展开。
2. 缺失引用、空必填值、循环引用、重复定义给出清楚错误。
3. 角色继承、独立连接覆盖和参数覆盖正确。
4. Agent、用户、裁判选择不同假连接时，分别收到正确模型、地址和 Key。
5. Chat/Responses 前缀正确，无重复前缀。
6. ASR/TTS 接收各自模型、地址和声线。
7. 启动参数、日志和结果序列化不泄漏假密钥。
8. 独立评分与生成入口使用相同解析规则。
9. judge-from-user 只影响裁判。
10. 模型或协议变化影响缓存标识，Key 轮换不影响缓存标识。

运行相关已有离线测试、修改文件的 Ruff 与差异格式检查。若适用目录要求 provider suite，运行其无需真实凭据的检查，保持 live provider 开关关闭。

不为本次迁移调用真实模型 API，不跑全量题目评测，不重评分历史文件，不新增生产验收脚本或逐轮校验流程。

## 13. 完成后的使用方式

- 在 config.env 维护可用供应商连接。
- 在 qw.env 选择被测 Agent 前后台连接。
- 在 realbench.env 选择评测与生成角色连接。
- 使用现有运行入口启动；需要不同配置目录时显式覆盖目录。
- 实验参数继续由运行 JSON、评分 YAML 或 CLI 管理。

不需要额外配置服务、数据库或管理界面。


## 14. 实施验证记录

已接通共用加载器、Agent/用户与语音、在线及独立诊断、后台回复裁判、主动请求生成、离线生成与重评分入口。当前统一示例和私有三文件已迁移，旧私有文件已在原目录保存权限受限的备份。

相关离线测试通过 176 项；provider suite 在关闭凭据后为 132 项跳过、0 失败。实际 smoke 启动入口的 dry-run 成功生成不含密钥的完整配置。修改的 Python 文件通过 Ruff lint/format，差异通过空白检查。没有调用真实模型 API 或重新评分历史结果。

新模式当前以角色文件为连接来源，用户模型的旧 CLI 连接参数与它冲突时显式报错；可通过配置目录切换不同角色文件。实际字段与命令见使用说明。

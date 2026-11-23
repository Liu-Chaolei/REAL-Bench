# 当前链路的 API 配置

当前 Gaia2 + Qwen Audio Agent、独立评分与题目生成支持三文件配置。默认目录为 `~/.config/qwaudio`，本机对应 `/hpc_stor03/sjtu_home/chaolei.liu/.config/qwaudio`。

| 文件 | 用途 |
| --- | --- |
| `config.env` | 供应商的 Key、Base URL、原始模型名 |
| `qw.env` | 被测 Agent 前后台的连接选择 |
| `realbench.env` | 用户、用户侧 ASR/TTS、评分与生成的连接选择 |

私有文件保存在仓库外。角色文件只引用连接池中的真实凭据。

## 启动

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1
```

smoke 默认使用统一配置与 Gaia2；旧 retail/其他配置需显式指定 `--domain` 和 `--config`。

指定另一目录时，向当前 run/smoke、metrics run、evaluate-trajs 或 gaia2-series 的具体子命令传入 `--api-config-dir /path/to/config-directory`。JSON/YAML 也支持 `api_config_dir`。目录覆盖在文件解析前生效。

```bash
tau2 metrics run data/simulations/RUN \
  --config examples/real-metrics.unified.yaml \
  --api-config-dir /path/to/config-directory
```

直接 run 使用同一个 Agent JSON 和 `--qwen-agent-config`。独立生成使用 `docs/gaia2-series.config.example.json`；author/reviewer 分别读取 AUTHOR、REVIEWER 角色。

新模式中，连接来源由 env 文件决定，shell 中残留的 OPENAI_* 不会覆盖角色选择。不要同时提供旧的用户连接、Agent environment 映射或字面评分连接；冲突时报错。旧单文件配置继续通过原有字段显式使用，且仍保留进程环境优先规则。迁移前的私有配置备份可以用于恢复旧模式；旧 JSON 不能假定已经精简的 config.env 仍包含旧角色变量。

## Agent 选择

qw.env 示例：

```dotenv
QWEN_AUDIO_REALTIME_PROVIDER=${QWEN_AUDIO_PROVIDER}
QWEN_AUDIO_REALTIME_MODEL=${QWEN_AUDIO_MODEL}
QWEN_AUDIO_REALTIME_BASE_URL=${QWEN_AUDIO_BASE_URL}
DASHSCOPE_API_KEY=${QWEN_AUDIO_API_KEY}
GAIA2_BACKEND_MODEL=${DEEPSEEK_MODEL}
GAIA2_BACKEND_API_BASE=${DEEPSEEK_BASE_URL}
GAIA2_BACKEND_API_KEY=${DEEPSEEK_API_KEY}
GAIA2_BACKEND_REASONING_EFFORT=low
```

当前评测桥接使用 Qwen Code，启动适配器将 GAIA2_BACKEND_* 映射到 OPENAI_MODEL、OPENAI_BASE_URL、OPENAI_API_KEY 与 QWEN_CODE_REASONING_EFFORT。OPENCODE_CONFIG_CONTENT 可以保留在 qw.env 供原有部署使用，但当前评测桥接不消费它，也不自动切换后台实现。

## 用户与评分选择

realbench.env 示例：

```dotenv
REALBENCH_USER_MODEL=${DEEPSEEK_MODEL}
REALBENCH_USER_BASE_URL=${DEEPSEEK_BASE_URL}
REALBENCH_USER_API_KEY=${DEEPSEEK_API_KEY}
REALBENCH_USER_API=chat

REALBENCH_JUDGE_MODEL=${GPT_MODEL}
REALBENCH_JUDGE_BASE_URL=${GPT_BASE_URL}
REALBENCH_JUDGE_API_KEY=${GPT_API_KEY}
REALBENCH_JUDGE_API=responses
REALBENCH_JUDGE_REASONING_EFFORT=low
```

模型保存原始名称。API 只接受 chat 或 responses，由适配层添加 LiteLLM 前缀。调用参数可用 REASONING_EFFORT、TEMPERATURE、MAX_TOKENS、NUM_RETRIES、TIMEOUT_SECONDS，未设置的参数不发送。

| 默认角色 | 继承它的角色 |
| --- | --- |
| USER | USER_DECISION、USER_GROUNDING、FOLLOWUP、HIDDEN_INTENT、ASR_HOTWORDS、ACTIVE_REQUEST |
| JUDGE | INTENT_GT、INTENT_PREDICTION、INTENT_EVALUATION、EXECUTION、INTERACTION、STATE_INTERACTION、FEEDBACK_NUGGETS、FEEDBACK_ALIGNMENT、REPLY_JUDGE |

AUTHOR、REVIEWER 需要各自完整配置；仅在启动生成时解析它们。其余可选用户角色在对应功能调用时解析，读取的是启动时的文件快照。

单独更换执行裁判时，完整填写 REALBENCH_EXECUTION_MODEL、BASE_URL、API_KEY、API 四项。仅更改参数则可以只填写，例如 `REALBENCH_USER_DECISION_TEMPERATURE=0.2`。继承不修改父角色。

`--judge-from-user-llm` 在新 run/smoke 模式下使诊断裁判与后台回复裁判复用 USER；它不会修改 Agent 后台。独立评分 YAML 如需同样行为，可设置 `api_judge_role: USER`。

独立 IntentConfig 可以设置 api_config_dir，三个阶段映射到相同的 INTENT_* 角色。旧 env_file 配置继续保持原有语义。

## 语音

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

Realtime、ASR、TTS 可以共用 Key，但地址、模型各自管理。ASR 地址由现有适配器转换成对应 HTTP 接口。语音模型必须符合所选适配器支持的协议。实验时序、采样、热词开关、音效继续通过运行 JSON/CLI 管理。

## 生成、备用连接与 Key 池

AUTHOR、REVIEWER 与其他文本角色采用相同的四项连接字段。调用超时等生成过程参数继续保留在 JSON 或 CLI；角色 env 中显式设置的调用参数优先于生成 JSON 的模型参数。

备用线路必须显式配置完整角色，例如：

```dotenv
REALBENCH_AUTHOR_FALLBACK_MODEL=${MINIMAX_MODEL}
REALBENCH_AUTHOR_FALLBACK_BASE_URL=${MINIMAX2_BASE_URL}
REALBENCH_AUTHOR_FALLBACK_API_KEY=${MINIMAX2_API_KEY}
REALBENCH_AUTHOR_FALLBACK_API=chat
```

REVIEWER 的备用角色同理。未配置则不自动猜测备用线路。`REALBENCH_AUTHOR_KEY_POOL_JSON='["MIMO_API_KEY"]'` 可为相同端点指定 Key 池，所有名称仍指向 config.env；并发和调度参数继续由生成配置控制。新的共享模式不再隐式要求 MIMO_API_KEY1、MIMO_API_KEY3 存在。

## 快照与记录

同一进程按规范化目录复用配置快照；编辑文件后重启对应运行生效。Key 只在调用时以私有参数传给客户端，不进入 JSON、结果或新模式的命令行参数。Agent 容器仅获得其需要的连接设置。

结果记录实际模型、非敏感地址、协议、参数、角色与 Key 引用名。诊断缓存和生成续跑标识反映模型与协议变化，单纯轮换 Key 不改变模型身份。历史配置快照可在没有私有文件的机器上读取；重新调用模型时仍需要相应私有配置。

迁移已保留 qw.env 选定的 Agent 后台，并将最近完整评测使用的 DeepSeek 用户/默认评分配置作为 realbench.env 初值。旧独立 Intent 的 GPT 配置保存在私有备份中；新模式的 Intent 默认继承 JUDGE，可通过 INTENT_* 完整角色显式更换。此配置选择不重写历史结果。

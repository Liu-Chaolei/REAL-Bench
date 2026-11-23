# 场景时间注入实施计划

状态：已实施。2026-11-22。具体交付和检查结果见 [实施记录](scenario-clock-injection-results.md)。以下保留设计及范围约定。

## 1. 目标与实现选择

解决 Gaia2 评测中，Qwen Audio Agent 前台读取电脑日期、后台读取题目日期的冲突。

采用「Agent 提供可注入的时间查询接口，适配层连接现有场景时钟」：

```text
前台 get_current_time
  → Agent 的可选时间查询回调
  → tau bridge 的时间适配函数
  → 现有 ToolBroker / tool_call / tool_result
  → System__get_current_time
  → ScenarioRuntime.time()

后台 System__get_current_time
  → 现有 MCP / ToolBroker / 工具执行链路
  → 同一个 ScenarioRuntime.time()
```

一致性来自两端读取同一个对象，而不是运行中不断比较两个时钟。

保留前台工具名称、参数、正常返回结构；不修改 Agent 的任务规划、对话策略、委派与权限逻辑。不新建模拟环境，不替换系统时间，不全局改写 JavaScript Date 或 Python datetime。

## 2. 已确认的代码事实

本次查看的 Agent 是实际测试镜像
`docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.3-real-metrics`
中的源码，记录的源码 revision 为
`d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9`。

Agent 文件路径以下均相对于镜像中的 `/opt/qwen-audio-agent`，不是当前 tau2 仓库中的文件。

1. `server/src/frontend/tools/features/core-tools.mjs`：`get_current_time` 调用 `currentTimeSnapshot(runtime.getClientContext())`。
2. `server/src/conversation/frontend-agent-context.mjs`：`currentTimeSnapshot()` 的 `now` 默认是 `new Date()`。函数已支持显式传入 `now`，返回 `iso_utc`、`local_time`、`time_zone`、`locale`。
3. `server/src/frontend/frontend-tools.mjs`：动态工具不允许与内置工具同名。即使隐藏内置定义，动态同名工具仍会被拒绝。
4. `createGatewayApplication()` 支持扩展工具来源，但没有本计划所需的时间查询回调。
5. tau2 的 `ScenarioRuntime.time()` 已返回场景起始时间加经过时间；Gaia2 的 `System__get_current_time` 已读取它。
6. `integrations/qwen_audio_agent/bridge.mjs`、`core.mjs` 已具备异步工具转发与结果关联能力。
7. 诊断证据已保存 `source.scenario_start_time` 和工具执行记录；交互评分输入没有传入明确的场景日期，并排除了 SYSTEM 操作。

已经进行过一次不调用模型的接口核查：动态注册 `get_current_time` 报同名错误，注册 `get_scenario_time` 成功。因此不采用同名外部覆盖，也不对运行中的工具处理函数做 monkey patch。

## 3. 本次范围及明确边界

本次完成：

- 前台时间工具通过适配层读取现有场景时间。
- 后台继续通过原有场景时间工具查询。
- 前后台公开时间说明一致。
- 交互评分获得场景日期与已记录的时间查询证据。
- 非场景环境保持默认真实时间行为。

以下是另外的能力，不纳入本次实现完成条件：

- **暂停场景时钟。** 当前 `ScenarioRuntime` 启动后按 monotonic 经过时间推进，没有运行中 pause/resume 接口。暂停 tick 不等于暂停场景时间。本次不改变推进语义，也不声称语音生成期间整个环境已经暂停。
- **原生提醒和定时任务。** Agent 的 `schedule_reminder` 仍使用 `Date.now()` 判断是否过期，原生调度也需要单独适配。此次只完成时间查询统一，不宣称原生提醒已支持模拟时间，也不静默移除该能力。
- **任意代码中的日期调用。** 后台 shell 的 `date`、任意 Python/JS 程序的系统时间不会被虚拟化。场景政策继续指定场景工具为业务时间依据。
- **重复追问的评分规则。** 本次仅补时间事实，不同时修改“用户不知道后继续追问”的判定规则，避免混合两项评分变化。

若之后扩展提醒调度，应让日期校验和触发器一起使用场景时钟；若扩展暂停，应同时处理场景事件调度和历史音频时间映射。两项都不能靠修改工具返回的日期完成。

## 4. Agent：增加一个可选、异步的时间查询依赖

### 4.1 接口

在 Gateway 构造入口增加可选参数 `getCurrentTimeSnapshot`，约定：

```typescript
type GetCurrentTimeSnapshot = (context: {
  timeZone?: string;
  locale?: string;
}) => Promise<{
  iso_utc: string;
  local_time: string;
  time_zone: string;
  locale: string;
}>;
```

普通运行不传参数。在最靠近时间工具的位置提供默认实现，仍调用现有 `currentTimeSnapshot()`；不要在五层分别构造默认值。

构造参数沿以下链路透传：

1. `server/src/app/gateway-application.mjs`
2. `server/src/app/frontend-runtime.mjs`
3. `server/src/voice/realtime-session-runtime.mjs`
4. `server/src/frontend/tools/tool-call-handler.mjs`
5. `server/src/frontend/tools/features/core-tools.mjs`

时间工具处理函数使用 `await` 调用依赖，再经现有 `sendOutput` 返回。保持原工具定义和成功结果的 `status: 'ok'`。

### 4.2 生命周期和隔离

- 回调归属 Gateway 实例，由当前 trial 的 bridge 创建。
- 不使用进程全局时钟对象、全局可变函数或跨 trial 缓存。
- 构造 Gateway、建立会话时不主动调用回调，避免评测循环尚未运行就等待工具结果。
- 仅在模型实际调用时间工具时读取时间。
- 对话重连继续使用该 trial 的回调，不重置场景时钟。
- 销毁 trial 时由现有 broker.close() 结束等待中的请求。

### 4.3 失败处理

- 已配置的时间来源失败时，返回普通工具错误，说明当前时间暂时无法获取。
- 不回退到真实日期，不返回上一次时间，不伪造查询成功。
- 使用现有工具失败输出及对话恢复路径，不新增“时间不一致导致整题评分取消”的分支。
- 底层连接断开、broker 超时等基础设施错误继续遵循已有处理，不借本次改动重写全局错误语义。

### 4.4 接口版本兼容

增加一个简单的模块导出能力标记，例如 `supportsCurrentTimeSnapshotProvider = true`，表示 Gateway 确实接受并使用这个参数。

bridge 只在启用场景时间时，于启动阶段读取一次。旧镜像缺少接口时明确报告版本不兼容，避免 JavaScript 静默忽略新增参数。无需调用模型、试运行题目或反复验证时间。

## 5. tau bridge：复用现有工具链路

### 5.1 启用条件

以 initialize 消息的公开工具列表是否包含 `System__get_current_time` 为依据。在当前支持的 Gaia2 场景中，该名称已由环境注册。

不从提示文本解析日期，不把题目 ID、隐藏目标、参考动作传给 Agent，不另外增加逐题时间配置。

有这个工具时注入回调；没有时省略参数，使用 Agent 的正常时间实现。

### 5.2 时间适配函数

建议新增 `integrations/qwen_audio_agent/scenario-time.mjs`，只承担查询和格式转换。

步骤：

1. 调用 `broker.call({ name: 'System__get_current_time', arguments: {} })`。
2. 读取现有 MCP 风格返回值；工具失败则返回失败路径。
3. 解析其 JSON，取 `current_timestamp`，确认是可表示的有限时间戳。这里是必要的接口数据解析，不做时钟审计。
4. 秒转毫秒，构造 `new Date(current_timestamp * 1000)`。
5. 复用 Agent 的 `currentTimeSnapshot({ ...context, now })` 格式化。适配函数通过参数接受 formatter，避免通用模块依赖固定镜像路径。

目前 bridge 已给客户端设置 UTC 与 en-US；保持这一约定。前台与后台可以使用不同显示格式，但指向同一时刻。

在 `bridge.mjs` 中，导入原格式化函数，创建当前 trial 的回调，传给 `createGatewayApplication()`。

### 5.3 不再创建第二套时钟

- 不在 Node 中按“场景起点 + Date.now 差值”计算当前时间。
- 不周期推送时间快照，不缓存当前时间，不启动对时线程。
- 不新增 HTTP 服务或控制消息类型。
- 不绕过 orchestrator，直接在 bridge 里执行业务工具。

读取请求走现有工具执行流程，会有原本的异步调度延迟。结果代表工具实际执行时的场景时间，不要求前后台先后两次查询的毫秒值完全相同。

### 5.4 启动与记录

在已有 ready metadata 中增加简单的时间来源说明，例如：

```json
{
  "frontend_time_source": "scenario",
  "frontend_time_tool": "System__get_current_time"
}
```

非场景运行记录 `host` 或沿用默认缺省语义。复用已有工具调用、返回值和场景 journal，不另建时间日志、检查报告或逐 tick 对账系统。

Python provider 和 discrete-time adapter 原有 `initialize`、`tool_call`、`tool_result` 协议可复用，预计不需修改它们。

## 6. 前后台公开时间说明

`Gaia2Environment` 已把场景起始日期、UTC、权威工具写入 policy；bridge 已把 policy 提供给前台及后台 QWEN.md。

仅在启用注入时，补一段简短说明：

- 前台 `get_current_time` 和后台 `System__get_current_time` 返回同一个场景时钟。
- “今天、明天、现在”按该时钟解释。
- 场景起始日期不是永久固定的当前时间；需要当前值时查询工具。

说明由能力启用条件生成，不对 retail 等非场景环境添加错误声明。

不改写原来的权限和路由提示，不增加逐轮纠正提示，不让用户承担时间解释工作。

## 7. 交互评分：传入时间事实，不增加评分拦截

修改 `src/tau2/diagnostics/interaction.py` 的 `build_input()`，增加可选 `scenario_clock`：

- `start_timestamp`：复用 `evidence.source.scenario_start_time`。
- `time_zone`：当前 Gaia2 约定 UTC。
- `authoritative_tool`：`System__get_current_time`。
- `observations`：仅选取现有记录中该时间工具的调用 ID、请求方、执行时间、结果和错误状态。

不要把所有 SYSTEM 事件一并解除过滤，也不要新增查询、回放业务动作或要求额外 ASR。

对 `conversation_evidence()` 的历史会话路径，若传入 task 本身有 scenario，可附带其公开的起始时间；没有工具证据时 observations 为空。不能根据新实现推断旧录音中的 Agent 已使用了正确时钟。

在 `interaction_prompt.md` 补充：

- 场景时间优先于主机日期和评分模型自身的当前日期。
- 起始时间只能作为场景背景，不能当作整场不变的当前时刻。
- 工具执行记录不等于前台已经收到或用户已经听到了结果；继续按当时可得证据判断。
- 缺少明确日期背景时，不自行用现实年份判定用户要求“已经过期”。

不改变 communicate、最终任务奖励、有效交互率公式以及不确定标签的处理。缺少时间证据不会取消整场评分。

更新 interaction 的版本标识；现有 implementation digest 已能区分输入/规则变化，不新增缓存校验机制。历史结果原样保留，不自动付费重评全部历史运行。

## 8. 文件变更清单

Agent 源码仓库：

- `server/src/app/gateway-application.mjs`：新增参数、能力标记、透传。
- `server/src/app/frontend-runtime.mjs`：透传。
- `server/src/voice/realtime-session-runtime.mjs`：传给 ToolCallHandler。
- `server/src/frontend/tools/tool-call-handler.mjs`：保存依赖并提供默认实现。
- `server/src/frontend/tools/features/core-tools.mjs`：异步查询及普通错误返回。
- 复用已有对应测试文件，覆盖注入到工具输出的路径。

tau2 仓库：

- `integrations/qwen_audio_agent/scenario-time.mjs`：新增小型适配函数。
- `integrations/qwen_audio_agent/bridge.mjs`：按工具能力注入、记录来源、补时间说明。
- `integrations/qwen_audio_agent/core.mjs`：仅当公开说明的组合需要调整时修改；保持 ToolBroker 的通用行为。
- `integrations/qwen_audio_agent/test/`：少量针对性离线测试，优先复用现有 fixture。
- `src/tau2/diagnostics/interaction.py`、`interaction_prompt.md`：补公开时间事实。
- `tests/test_diagnostics/` 的现有相关测试：验证输入投影，不调用评分模型。
- `integrations/qwen_audio_agent/README.md`：说明注入接口和当前覆盖边界。
- 本次运行使用的配置文件：切换到包含新 Agent 源码和 bridge 的明确镜像版本。

预期不修改 ScenarioRuntime、Gaia2 业务工具、tick 循环、用户生成、语音提供商及基础通信协议。

## 9. 镜像与落地顺序

1. 在实际 Agent 源码仓库增加通用注入接口；实施前确认当前 checkout 与已检查的镜像 revision 的关系，只读取明确的项目路径，不扫描共享目录找仓库。
2. 完成 bridge 回调及公开说明，让调用走现有工具链路。
3. 补交互评分输入的时间背景。
4. 运行第 10 节中的针对性离线检查。
5. 按项目现有方式构建包含新源码的镜像，记录真实 source revision 与 image ID。
6. 只更新选定运行配置的镜像版本，说明这次是环境时间接入能力的变化。

现有 `build_image.py --base-image` 要求 Agent 源码 revision/hash 完全相同；本次修改 Agent 源码后，不能直接走“只更新 bridge”的复用方式。使用正常构建流程及可用 Docker 构建缓存，不伪造旧源码标识，也不为此另建构建系统。

构建要求干净且可追溯的源码快照；遵循源码仓库已有提交/构建约定，不自动提交其他工作区改动。不覆写旧镜像标签，不自动发布到远端注册表。

实际落地调整：完整构建遇到新的基础镜像下载缓慢，最终本地镜像复用原 `0.5.3-real-metrics` 的依赖环境。构建前确认原源码归档校验值和两个 integration 依赖锁一致，源码差异仅为本次 6 个 `.mjs` 文件，再覆盖这些文件、当前 bridge 与真实的新 provenance。没有放宽 `build_image.py --base-image` 的规则，也没有增加常驻构建机制；完整源码构建仍可使用上述现有脚本。本地构建配方和基底 image ID 记录在实施记录中。

## 10. 轻量检查和验收

### 离线检查，仅三组

1. **时间工具注入路径。** 给构造链注入固定时间，调用前台工具并断言输出使用注入值；缺省依赖仍使用正常实现。这同时验证参数没有在中间层丢失，不为每层透传分别写测试。
2. **bridge 回调。** 用已有 broker fixture 返回两次不同场景时间，确认每次读取、单位转换、错误不回退；请求发出和结果返回复用同一异步链路。
3. **评分输入。** 使用小型保存证据，确认场景背景和时间工具记录进入输入；缺少这些字段的旧证据仍可构造输入。

相关文件的语法、lint 和项目明确要求的现有检查按正常开发流程执行。不得把这些测试装进每道题的运行流程。

### 在线观察

不把新跑一整道 0535、全指标重评或五题回归作为此次改动的强制验收门槛。下一次用户要求测试时，正常跑题并利用已有 trace 观察时间工具结果即可。

如果实施过程中确有离线测试不能解决的真实接入疑点，最多做一次限定范围的接入 smoke；不连续跑题寻找理想结果，不以评分提升作为时间接口正确性的证据。

### 运行时不增加的负担

- 无逐 tick 对时、双源比较、时钟漂移阈值。
- 无为验证日期而新增的 LLM 请求。
- 无每题额外前后台对话或时间校准阶段。
- 无后台巡检、额外任务进度轮询或重复工具读取。
- 无因缺少日期证据而取消其他评分的规则。

只保留必要的接口解析和一次启动兼容判断。模型每主动查询一次时间，才产生一次现有场景时间工具调用。

## 11. 完成定义

完成后可以明确陈述：

> 在启用场景时间的评测里，Qwen 前台和后台的指定时间查询工具读取同一个现有场景时钟；前台不再通过该工具获取主机日期；交互评分拥有同一场景时间背景。

不能由此宣称原生提醒已完成时间适配、任意代码都读到模拟时间，或用户生成期间整个系统已暂停。也不能保证 Agent 不再出现其他原因的重复追问。

交付包括源码改动、镜像/配置接入、少量离线检查结果和上述边界说明；不以重跑大量题目或增加评测拦截来替代实现。

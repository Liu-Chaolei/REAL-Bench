> Historical implementation plan. Active async definitions now live in ordinary Gaia2 tasks; see [current architecture](persistent-tasks.md).

# Qwen Persistent Task 实施方案

日期：2026-11-13。依据当前工作区代码及 `persistent-task-qwen-plan.md`。
本文是待执行方案，新增文件名、接口、配置及命令均为拟议设计，不代表已经实现。

## 1. 交付目标与范围

第一阶段交付：在真实 Qwen Audio Agent 中，依据真实后台任务状态释放用户追问，记录原生任务操作、结果与实际播出的回复，完成一题端到端验证，再验收十题。

限定为 Qwen、英语、同一通话、同一 Gaia2 stage。通用模型保持 provider-neutral；不实现 GPT Live live 接入、跨通话恢复、重试、ASR 干扰或 100 题扩展。确定性 replay 是第一阶段必要验收，不扩展复杂 replay 矩阵。

必须成立的约束：

1. live 不挂载 `PersistentTaskTools`，不提供 τ 伪造的 query/modify/cancel 工具。
2. 状态与操作来自真实 Qwen runtime；模型说“完成”、ACP 接受 prompt、语音 response 结束均不足以证明后台任务完成。
3. benchmark 任务身份为 `source_task_id + source_stage`；Qwen 私有 task ID 只保存在运行产物，不写入题目定义。
4. benchmark 私有绑定规则、参考答案、触发条件和评分期望不进入 Agent prompt、MCP schema 或用户可见上下文。用户只收到实际释放的自然语言请求。
5. 业务 MCP 调用继续由 τ 的既有链路执行，状态 observer 不执行、重试或替代业务操作。
6. 运行证据与模型表现分开：基础设施证据不足要明确标记；模型不创建任务、不查询或查错任务要保留为测量结果。

## 2. 现状及必须补强的基础代码

已有但尚未形成 live 闭环：`BackendTaskObservation`、Qwen payload adapter、identity binder、状态 projector、state trigger scheduler，以及 provider 的后台事件缓存。

代码核对发现：

- `bridge.mjs` 只有在 `diagnostics_enabled` 时调用 `observeBackend`；persistent task 需要独立启用，不能依赖 REAL metrics 配置。
- `core.mjs/observeBackend` 主要观察 submit/update/prompt/steer；没有确认真实任务 lifecycle 和 native query/result 的权威事件来源。
- `BackendTaskProjector.ingest` 按 status 字符串直接赋值；没有完成结果校验、时间/序号排序、终态保护，且会自行根据 observation 建立身份。
- `TaskIdentityBinder` 选首个匹配规则；没有完整检查 create/submit 起点、activation、多个候选规则和一个角色的多个候选任务。
- `BackendTriggerScheduler` 缺少完整的 release/delivery trace、过期规则与终止原因。
- full-duplex `step()` 每个 tick 调用 `notify_turn_boundary()`；tick 边界不能直接当作语音轮次边界。
- 现有 `PersistentTaskRuntime` 连接的是 in-memory service 与 milestone scheduler；live 需要独立 coordinator，不能无意接回 synthetic state。
- `evaluate_backend_operations` 目前主要匹配 request、operation 与 source 身份，尚未严格校验成功、backend ID、state/result。
- `evaluate_summary` 的非空回复检查仅适用于原型；`finalize_reward` 目前只要存在 summary 就乘 persistent reward，需要改成显式任务配置和 reward basis 控制。
- 当前十题仍为 synthetic sidecar。说明文档中的操作数量、状态数和文件名也有陈旧内容，需要随迁移统一。

原有离线 service、milestone fixture 和测试可保留，但与 live 配置使用不同 schema/version；新路径拒绝误载旧 sidecar。

## 3. 阶段 P0：确认 Qwen 原生能力，先解决可行性

这是后续实现的进入条件。先检查实际使用的固定 Qwen source revision 和容器 image，再确认下面的证据矩阵。

| 能力 | 必须找到的证据 | 不足以作为证据的事件 |
| --- | --- | --- |
| 创建/提交任务 | 后台 task ID、所属 session、真实 create/submit 记录 | 口头承诺、无 ID 的 prompt |
| queued/running/waiting | task store、runtime state transition 或等价的原生状态通知 | `accepted=true`、任意工具开始 |
| completed | 原生终态及对应结果记录/结果引用 | frontend response.done |
| failed/cancelled | 原生任务终态、错误/取消确认 | 单次工具失败、用户提出取消 |
| query/result | 原生工具名、call ID、目标 task ID、请求与返回 | 单纯订阅状态推送 |
| modify/cancel | 原生操作与确认、允许修改的字段、取消生效边界 | 接受 steering 即假定修改成功 |
| 多任务关联 | 顶层/子任务标识、父子关系及可区分的业务关联 | 按返回顺序猜测 primary/secondary |

优先查已有 Gateway task/progress 事件：当前 `onEvent` 已转发部分事件为 `gateway_event`，不能只看 `diagnostic_backend`。若 Gateway 不完整，再在原生 task runtime 的权威更新点增加只读观察。

实施要求：

1. 使用 build metadata 和现有配置定位固定 revision；仅在已批准的明确 Qwen 路径内检查源码，不扫描父目录寻找仓库。
2. 给每个状态/工具记录实际文件、事件名、字段和一段脱敏样例，产出 `qwen-backend-capabilities.json`。
3. 确认 observer 挂载层：frontend task runtime、BackendPort、ACP 各负责什么。后台任务身份以真实拥有任务生命周期的层为准，不能自动把 ACP session 等同于 persistent task。
4. 确认 observer 不改变原生方法返回值、promise、异常、权限及取消行为。
5. 若某能力不存在，先报告 unsupported。可以补被动观察；新增 Agent 任务工具/任务生命周期属于被测系统能力变更，必须单独版本化，不能包装成 benchmark observer。

P0 出口：至少能真实观察任务创建、running、完成/失败，以及 Agent 原生 query。若 modify/cancel 或多任务身份不可观测，先完成可支持的闭环，十题完整验收保持未完成，不能用 fake event 补齐。

## 4. 阶段 P1：冻结事件、身份和时间契约

建议把 `backend.py` 中协议模型逐步迁入 `backend_models.py`，保留兼容导出；严格的可序列化模型使用 Pydantic。模块拆分随实际复杂度执行，不为拆分而增加抽象。

### 4.1 事件 envelope

统一事件至少包含：

```json
{
  "schema_version": "persistent-backend-event-v1",
  "provider": "qwen",
  "session_id": "runtime-session-id",
  "event_id": "stable-event-id",
  "sequence": 12,
  "kind": "task_state_changed",
  "backend_task_id": "runtime-task-id",
  "parent_task_id": null,
  "call_id": null,
  "provider_timestamp": 123.4,
  "provider_clock": "bridge_monotonic",
  "received_monotonic": 456.7,
  "scenario_time": 1800000000.2,
  "status": "running",
  "result_ref": null,
  "evidence_source": "native_task_runtime",
  "raw_ref": "backend-events.jsonl#12"
}
```

`kind` 明确区分 task_created、task_state_changed、native_operation_started/completed/failed、task_result、agent_turn_completed 和普通 diagnostics。submit/prompt 记录仍保留，但不会自动变为 running。

原始 payload 保存在 evaluator 侧的脱敏 journal，并以引用关联标准事件。event_id、sequence、session/task/call ID 用于去重与关联；不把大块 raw 复制进每个 summary。

### 4.2 时间契约

- bridge `performance.now()`、Python monotonic、scenario epoch、音频 session clock 是不同时间域。
- provider 接收时记录 Python monotonic；live 调度第一版以接收时刻映射的 scenario time 为准，明确记录这一语义，不直接减 bridge timestamp。
- 原始 provider 时间用于审计；同一来源 sequence 用于顺序检查。需要跨进程事件时间校准时另存校准记录，不能隐式假定同源。
- 请求条件、入队、语音投递使用同一 scenario clock；需要回复音频窗口时通过现有媒体时间映射转换。
- scenario 启动前的观测保留并缓存；在时钟启动时记录如何激活已有任务状态，不把 boot 时间误算成用户请求后的等待时间。
- replay 使用保存的 normalized timestamp、sequence 和 boundary 事件；不重新读取当前系统时钟。

### 4.3 去重与异常

同一 lifecycle event 重传只投影一次；同一 call 的 start/result 是同一操作的不同阶段。使用 session + event_id 或明确的稳定来源键去重，不把两条相同文本当作同一业务调用。

缺字段、未知事件、时间无效、身份不明确都形成结构化 evidence issue。正式验收要求必需证据完整；未知 payload 不猜测成功。

## 5. 阶段 P2：打通被动观测通道

修改入口：

- `integrations/qwen_audio_agent/core.mjs`：按 P0 证据增加 lifecycle/native operation observer。
- `integrations/qwen_audio_agent/bridge.mjs`：增加独立 backend observation enable/capability handshake；同时保留 diagnostics。
- `qwen_agent/provider.py`：接收、记时、脱敏、保存 journal，单独排入 backend queue。
- `qwen_agent/discrete_time_adapter.py`：在拥有 background event loop 的线程上 drain，解析为统一事件。
- `DiscreteTimeAudioNativeAgent`：暴露可选 drain/capability 入口，orchestrator 不直接访问 Qwen provider 私有字段。

新增配置建议为 `--persistent-task-config <path>`，独立于 `--metrics-config`；配置解析后要求 gaia2 + qwen_agent + 支持的模式。参数必须贯穿 CLI/run config/runner build/checkpoint，不能只加到 smoke 脚本。

bridge initialize 仅接收观测开关、协议版本，不能接收 source task、trigger、expected operation 等 benchmark 私有字段。readiness 返回实际 supported events/operations/version。

provider 当前 list 缓存改为可安全跨线程消费的队列，或通过 `BackgroundAsyncLoop.run_coroutine` 在 owner loop drain。不要在接收线程 callback 中直接修改 ScenarioRuntime。

队列容量受控；正常回调不阻塞音频。队列溢出必须记录 evidence gap 并终止/降级该次验收，不能静默丢失后继续评分。关闭时执行 final drain，存 journal 与 evidence completeness。

验收：无 metrics config 时也能观察；事件不进入 media queue/对话历史；关闭开关后普通 Gaia2 行为保持兼容；observer 不改变 Qwen 的结果与错误。

## 6. 阶段 P3：严格身份绑定和状态投影

### 6.1 TaskIdentityBinder

输入为新 sidecar binding rules、当前实际投递的 stage、原始 activation event 的证据，以及 task_created/submit observation。

绑定规则至少包含 role、source_task_id、source_stage、identity_source、activation_event_ids、顶层/子任务约束及必要的明确关联规则。activation 只限定候选任务，不产生 backend 状态。

步骤：

1. 原始阶段请求实际投递后激活该 stage 的 binding rules。
2. 暂存来自尚未绑定 task 的状态，等待真实创建记录；不要丢掉先到的 state，也不要直接绑定到第一个 role。
3. 求所有匹配候选；恰好一个才绑定。零候选保留 unbound；多个候选标记 ambiguous。
4. 检查同一个 backend ID 不跨 source/stage 绑定；一个 role 不接受多个无法区分的 backend task。
5. 内部子任务默认排除，只有题目明确声明的角色才可进入绑定。
6. 保存 binding journal：证据 event、规则、成功/拒绝原因和时间。

同一 stage 的多个原生后台任务使用 `role` 区分，source task/stage 仍是业务来源；不能给它们虚构新的 Gaia2 source ID。状态 scheduler 的 key 至少包含 source/stage/role，避免当前仅按 source/stage 索引导致覆盖。

### 6.2 BackendTaskProjector

projector 只处理已绑定身份，不再自行猜测绑定。保留现有 canonical 状态；映射规则按固定 Qwen revision 版本化。

- queued → running/waiting → completed/failed/cancelled；running 与 waiting 可往返。
- 允许有证据的快速跳转，例如初始 snapshot 直接为 completed，不要求补造中间 running。
- completed 要求真实任务结果或可解析的权威 result_ref；结果为空但合法的成功操作也需有明确原生完成记录，不能用文本是否非空判断业务成功。
- completion 与 result 分两条事件到达时保存 pending terminal，等关联结果后确认；不会先触发 completed 再补证据。
- 按 sequence/time 检测乱序；旧事件不覆盖新状态。相同状态不重复触发。
- 终态不可被普通迟到 running/queued 覆盖；冲突终态产生 evidence issue。第一版不支持任务重新打开或重试世代。
- 无 status 的 prompt/steer 只作为证据，不覆盖已知状态为 unknown；明确无法解释的状态标记 issue，停止依赖该状态的释放。
- failed 不等于某次业务工具失败；cancel_requested 单独作为 operation pending 信息，收到真实停止确认后才进入 cancelled。

验收包括 terminal/result 双事件、合法空结果、乱序、重复、终态冲突、内部任务以及多角色身份隔离。

## 7. 阶段 P4：新增 live coordinator，接入真实调度

建议新增 `persistent_tasks/runtime/coordinator.py`，组合 binder/projector/scheduler/operation recorder/reply window；保留旧 `runtime.py` 供离线 fixture。

责任划分：Gaia2 loader 提供 evaluator-private scenario；runner 选择 sidecar 并构造 coordinator；orchestrator 驱动时间、观测 drain、轮次和投递；Qwen adapter 只提供观测；evaluator 只读封存产物。

拟议调用契约：

```python
coordinator.observe_backend(events)
coordinator.source_event_completed(event_id, result, timestamp)
coordinator.advance(now, boundary)
coordinator.user_delivery_started(request_id, timestamp)
coordinator.user_delivery_completed(request_id, timestamp)
coordinator.agent_delivery(fragment)
coordinator.finalize(timestamp, termination_reason)
```

### 7.1 每个 tick 的处理

1. 在 orchestrator 线程 drain backend observations；journal 后依次绑定和投影。
2. 更新原始阶段投递与 activation evidence。
3. 处理 trigger condition、delay、状态失效和请求截止时间。
4. 计算真实可投递边界，释放至 `runtime.user_inputs`；再交给既有 `user.queue_scenario_input`。
5. 原有音频、业务 MCP 执行和 playback receipts 流程继续运行。
6. 本 tick 收到的新观测在下一轮处理，记录其实际可见时间；不倒填更早的 queued_at。
7. 语音实际投递回调更新 started/completed，Agent 实际播放的 ASR fragment 进入对应窗口。

runner 构建时保存版本与配置摘要；simulation.info 只附 summary/artifact 引用。关停顺序为：停止新增工作、drain 现有事件、完成播放/ASR finalization、封存窗口和 trace，再执行评分；异常结束也保存已收集证据。

### 7.2 Trigger 语义

每个 request 声明 role、目标 state、enter transition、delay、after_turn_boundary、max_wait/deadline、失效策略。状态进入时间记录为 condition_met_at，最早 ready 时间为 condition_met_at + delay。

request 状态为 pending → condition_met → ready → queued → delivery_started → delivered；也可以结束于 expired、invalidated、not_reached。每个 request ID 只能入队一次。

第一版 running 查询默认要求入队时仍在 running；若 delay/轮次等待期间进入 completed，则标记 expired_state，不能继续问“仍在执行的任务”并按 running 评分。不要人为拖慢后台。需要“刚才的状态”回溯题时使用单独题型和明确措辞。

completed/failed 查询可在终态保持期间等待语音边界。取消/失败后，不适用的尚未入队请求失效；专门查询 failed 的请求仍可触发。已经入队或开始播出的请求不静默删除，保留实际投递及失效记录。

### 7.3 正确的语音轮次边界

修正当前每 tick `notify_turn_boundary` 的做法。区分“时钟 advance”和“communication boundary”。边界根据实际状态合成：

- 当前用户 utterance 已播完，且没有上一个 persistent request 的待投递音频；
- 当前 Agent 回复的播放已结束/取消，输出缓存已排空；有 turn-end 信号时同时使用；
- 无正在处理的 frontend 回复，不把一句话中间的短暂停顿当成回复结束；
- 后台 persistent task 可以继续运行，不能要求整个 backend idle，否则 running 查询永远不触发。

普通 pending business call 与当前 frontend reply 的关系需明确：原子业务操作执行/结果投递的中间时刻不插入追问，但不能等待完整后台 workflow 完成。优先使用现有 has_pending_output/playback/current chunk 信息，补足缺失的 frontend activity 能力接口。

若后端没有可靠 turn-end 信号，先明确使用的播放排空和静默判定规则、版本及局限，通过连续音频用例验收；不能把任意 tick 或 response.done 宣称为真实任务/轮次完成。

half-duplex 共用 state/release 核心，在已知的完整消息边界调用；第一阶段真实 Qwen 验收仍只做 full-duplex。

### 7.4 与 Gaia2 多阶段控制协调

有 pending persistent request 时，不能由 `ScenarioStages.stop()` 立即跳到下一 stage 或结束通话。先处理当前 stage 的必需 request，直到 delivered/handled 或明确过期/失败，再进行 stage transition。

建议由 coordinator 返回 `stage_can_advance` 和阻塞原因，避免在 orchestrator 放多套独立条件。设置 stage/request timeout 防止无限等待；同 tick 的释放次序固定，persistent request 与下阶段主请求不能同时争用用户输入队列。

## 8. 阶段 P5：捕获 native operation，建立回复窗口

新增 `operations.py`，在 P0 确认的真实工具/任务接口处解析，按固定 provider revision 映射 query/result/modify/cancel/association。

OperationRecord 保存：call ID、原生工具名、backend task ID/role/source 身份、started/completed、arguments、result/result_ref、success/error、active request、关联 lifecycle event。status push 不算 query，业务 read 工具不自动算 task query。

request 归属以用户语音实际开始投递为起点，记录完整投递时间；操作在 start 时冻结 request_id，result 即使在后续窗口返回也不能改挂到新请求。投递开始前的自动查询属于 unbound operation，不满足该题。第一版避免用户输入重叠，部分投递仍保留并标记不完整。

modify 要校验字段和值以及真实确认；cancel 要区分提出、接受和真实停止。查询取消状态所必需的额外调用需要题目明确允许，不把每条 observer event 都当成一次重复操作。

真实修改/取消会改变原始业务目标：新题必须声明哪些原始要求被替换或停止。派生 runtime/evaluation overlay 记录被授权的变更与对应检查；不能继续用原始“必须完成的写入”判取消失败，也不能直接跳过整个 Gaia2 reward。修改后仍适用的安全约束和既有已完成效果继续检查。该 overlay 纳入版本、hash 和 replay。

回复窗口从 request 实际开始投递到下一个请求开始投递或通话结束。用于语义评分的内容必须来自窗口内真实播放的 Agent 音频/ASR，不能使用 caption、未播放缓存或 backend 结果冒充回复。最终 ASR fragment 按音频区间归属，不按回调到达时间归属；跨界片段不能可靠切分时标记 alignment issue。

## 9. 阶段 P6：评分和统计

拆成确定性 evidence validation、operation/state/result check，以及离线 semantic reply judge。不要在 `finalize_reward` 的简单同步后处理中临时启动带网络的 LLM judge。

每个请求保留三部分：

1. **Operation**：已实际投递；操作类型、唯一逻辑调用、backend identity/source/role 正确；成功结果可追溯。按题目允许的操作集合检查额外调用，observer 重传不算重复操作。
2. **State/result**：查询时对应状态正确；结果来自该真实任务；modify 字段生效；cancel 停止确认；不能用最终 completed 状态倒推早先查询也 completed。
3. **Reply**：在正确窗口回答，状态/结果内容与 native evidence 一致，满足用户要求。复用现有 semantic communication judge 的配置与调用机制，补上下文支持；若需判断“与结果不矛盾”，增加明确的 evidence-grounded criterion，不能只做 substring 或非空检查。

judge 输入仅有用户请求、窗口回复、允许的真实结果证据和评分规则。保存模型/参数/版本、证据 hash、判决和原因；重评缓存键包含这些输入。LLM 网络失败标记 grading_error，不把基础设施错误当作模型回答错误。

总体：`persistent_reward = operation × state_result × reply`。保留三项与逐题细节。普通 Gaia2 reward 单列；仅显式启用的派生 persistent benchmark，且 reward basis 配置包含 PERSISTENT_TASK 时，才组合 final reward。summary 的存在本身不能触发乘法。

触发覆盖要独立统计：expected、activated、condition_met、queued、delivery_started、delivered、answered。没有被实际问到的请求不能按空 expected list 得满分。

- 模型没有建立要求的后台任务、结束过早、查错目标：在约定的任务能力评测中作为失败，保留原因。
- running 窗口因真实执行太快而消失：记录 expired/trigger coverage，不伪造触发；正式选题阶段替换不稳定题。评测运行时不得事后删除这类模型结果来抬高分数。
- observer 断流、身份协议不兼容、ASR 证据缺失：标记 infrastructure/evidence unavailable，报告数量及原因，不暗算成成功或零分混入有效样本。
- 行为失败与测量错误均保留在全量报告；能力分数必须伴随覆盖分母和有效样本数。

## 10. 阶段 P7：重造数据，先一题后十题

旧 `persistent_tasks/` 保留作为 legacy fixture，live 使用新目录，例如 `persistent_tasks_qwen_v1/`。不覆盖已封存原始 Gaia2 tasks.json，不手填原始 manifest hash。

新数据包含：manifest/schema version、task binding、request/trigger、expected native operation、state/result/reply checks、业务目标 overlay、来源 provenance 与验收 trace 引用。运行产生的 backend IDs 与实际结果只在验收产物中，不能作为未来运行的常量。

示意配置：

```yaml
version: qwen-persistent-v1
scenario_id: qpt_0001
source_task_id: <existing-gaia2-id>
source_stage: 1
bindings:
  - role: primary
    identity_source: backend_task_created
    activation_event_ids: [<existing-stage-event-id>]
requests:
  - request_id: request_1
    text: How is that task progressing now?
    trigger:
      task_role: primary
      state: running
      transition: enter
      delay_seconds: 1
      after_turn_boundary: true
      require_state_at_queue: true
    expected:
      operation: query
      target_role: primary
      state: running
      reply: accurately_report_native_status
```

schema validator 检查 source/stage/event 真实存在、role 唯一、request ID 唯一、trigger 与 expected 兼容、必要能力可用、overlay 完整。数量配额来自 manifest，不在 validator 硬编码十题分布。

第一题选择原则：原始工作流能自然产生真实异步任务，running 时间足以跨过 delay 和语音边界，前台有机会回应进度追问。原来的 pt_0001 不自动成为新第一题；若其工作流太短应换题，不通过 sleep、阻塞 MCP 或伪造 queued 状态延长任务。

第一题逐步验收：真实创建 → 明确绑定 → running → delay → 真实轮次边界 → 用户语音投递 → native query → 真实结果 → 实际回复 → 离线评分 → replay 相同。

通过后扩展为：2 running query、2 completed query、2 failed query、2 modify、1 cancel、1 多任务关联。

失败题要求来自真实且可复现的原生任务失败；单次工具错误、断网或 τ 注入 fake failed 不能代替。只有在已有 benchmark 环境支持确定性、合法业务失败条件时才据此构造。若固定 Qwen 无法稳定产生所需任务失败，保留未支持状态，不宣称十题验收完成。

每题需保存：raw backend journal、normalized events、identity bindings、state transitions、release/delivery trace、native operations、reply windows、business overlay、评分、source/image/config/judge versions 与完整性/hash。真实验收 trace 与 key-free 合同 fixture 分开标记。

## 11. 阶段 P8：最小测试、smoke 和交付门槛

仅增加覆盖真实失败模式的测试，复用现有 fixture；不要为每个简单字段增加镜像测试。

### Key-free 核心测试

- create/state 乱序、ambiguous binding、跨 task/stage/role、内部任务隔离。
- 重复/过期事件、终态保护、completed 无结果/合法空结果、未知诊断不降级状态。
- delay、真实 boundary、状态提前改变、重复入队、failed/cancelled 失效规则。
- native operation start/result 合并、目标错误、失败调用、unbound、跨窗口返回和允许的额外调用。
- semantic judge 用冻结判决 fixture；空回复失败、错误状态失败、ASR finalization 不能错归窗口。
- 同一保存的事件与 boundary journal，replay 得到相同 binding、state、release 顺序和确定性检查。

### Bridge/provider/orchestrator 集成测试

- JS observer 保持原生 promise/异常语义，订阅正常清理，diagnostics off 时 persistent observation 可用。
- Python 后台事件与媒体隔离、跨线程 drain、安全关停、重连/下一 trial 无旧事件泄漏、证据缺口报告。
- frontend 连续播放时不释放；播放完后能释放，而后台任务仍在 running。
- 不因用户 STOP 提前跳 stage；同 tick 只释放一个确定顺序的输入；stage/request timeout 能结束。
- feature off 时普通 Gaia2、Qwen、REAL diagnostics 与 transcript 回归保持通过。

### 真实验收顺序

1. 检查固定 image、凭据引用、capabilities 与 observer handshake，输出 dry-run，不调用业务模型。
2. 跑一条 capability probe/真实 smoke，验证上述十个事件环节；保存失败产物，不自动换题重跑。
3. 只修复实际出现的问题，复用已保存 trace 做确定性检查，必要时再跑该一题。
4. 验证其余九题各一条必要真实路径；多任务题至少两个可区分真实任务；modify/cancel 必须有实际确认。
5. 所有正式题有真实 trace、正确触发和操作证据、回复判决、replay 一致后，才能标为 verified。

代码通过但 smoke 没跑，交付状态写 wired/unverified；真实后端不能提供某能力则写 unsupported。不得把离线测试通过数量当作 live 验收。

拟议检查命令（新文件/入口实现后才能使用）：

```bash
UV_FROZEN=1 .venv/bin/python -m pytest tests/test_persistent_tasks -q
UV_FROZEN=1 .venv/bin/python -m pytest tests/test_voice/test_audio_native/test_qwen_agent.py tests/test_voice/test_audio_native/test_qwen_agent_domains.py -q
UV_FROZEN=1 .venv/bin/python -m pytest tests/test_streaming/test_scenario_voice.py tests/test_streaming/test_scenario_series.py -q
node --test integrations/qwen_audio_agent/test/core.test.mjs
make check-all
```

另外运行新增的 backend runtime/streaming/key-free replay 用例。数据变更后按仓库要求 `tau2 check-data`；影响 Gaia2 runtime digest 时走现有 verify/seal 流程，不能手工同步 hash。真实 smoke 通过 gated live 入口执行，命令记录配置引用，不打印密钥。

## 12. 实施批次与依赖

| 批次 | 主要文件/产物 | 完成门槛 |
| --- | --- | --- |
| A：能力确认 | capabilities 报告、固定 revision 事件样例 | 明确真实状态和 native 工具来源 |
| B：协议与通道 | core.mjs、bridge.mjs、provider、adapter、可选 agent 接口 | 独立观测、媒体隔离、时间与 journal 完整 |
| C：身份与状态 | backend models、binder/projector、backend fixture | 拒绝歧义、乱序/终态/结果规则通过 |
| D：live runtime | backend_runtime、runner/config、orchestrator、ScenarioStages | running 条件经真实边界进入用户输入队列 |
| E：证据与评分 | operations、reply windows、evaluator/outcome、overlay | 操作/状态/回复分别可审计，普通任务不受影响 |
| F：第一题 | 新 schema/loader、smoke 入口与 trace | 一题真实闭环与 replay 通过 |
| G：十题与文档 | 九题数据、manifest、验收报告、旧文档迁移说明 | 十题必要真实验证通过，无 synthetic live 输入 |

A 是硬前置；B/C 完成后接 D；E 与 F 数据设计需要一起收敛，F 通过前不批量制作剩余九题。能力确认耗时取决于实际 Qwen 源码和接口，因此不在确认前承诺固定工期。

最终交付：可运行的 gated 配置/命令、版本化十题 sidecar、完整实现、针对性测试、真实验收报告、可重放的脱敏证据，以及列明 supported/unsupported 的能力表。100 题扩展必须在此之后另行评估。

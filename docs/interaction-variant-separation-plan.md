# Gaia2 交互变体职责拆分计划

状态：实施中；场景机制 manager、独立输出、评分入口和请求来源已落地，公共证据层抽取暂缓。

## 1. 目标

当前 `InteractionVariantRuntime` 复用了 active persistent-task 的运行时基础设施，但同时负责后台事件观察、用户请求注入、任务绑定和结果评分。这样会让交互变体看起来像普通的后台任务指标。

本计划的目标是把以下职责区分开：

- **场景机制**：改变测试过程中会发生什么，例如追加临时请求、修改请求或取消请求。
- **运行证据**：记录 Provider 提供的后台任务身份、状态、操作和时间。
- **评估结果**：在运行结束后判断交互变体是否完成。
- **普通指标**：继续评估普通 persistent task，不被交互变体的流程控制混淆。

第一轮只整理职责、生命周期和结果结构，不改变已有变体的业务含义，也不在正式评测中增加重型验收流程。

## 2. 当前问题

### 2.1 变体被放在普通指标入口下

`run_simulation()` 发现交互变体后，会自动准备空的 `active_metrics_config`。随后 `ActiveMetricManager.install()` 发现变体并安装 `InteractionVariantRuntime`。

这使得一个会改变用户请求和对话流程的场景机制，看起来像普通 active metric。

### 2.2 结果名称会伪装成 persistent task

`InteractionVariantRuntime` 继承 `PersistentTaskMetricRuntime`，并继承了 `name = "persistent_task"`。因此变体结果可能出现在：

```text
simulation.info["active_metrics"]["persistent_task"]
```

读结果的人无法直接判断这是普通后台任务指标，还是交互变体运行记录。

### 2.3 同一个运行时同时推动对话和计算分数

变体 runtime 的 `advance()` 会观察后台任务并把新的用户请求加入 `ScenarioRuntime.user_inputs`。同一个对象的 `finalize()` 和 `score()` 又负责生成运行摘要和完成分数。

这会把“什么时候追加请求”和“Agent 做得是否正确”放进同一个生命周期对象。

### 2.4 变体会修改运行时任务视图

`apply_variant()` 会复制源任务并构造运行时副本。它会修改指定阶段的初始用户请求；`addtask` 还会增加用户事件和参考事件。

这是合理的场景构造行为，但需要明确它属于测试场景机制，不应被误解成普通评分器修改了任务数据。

## 3. 目标架构

```text
ScenarioRuntime
├── ScenarioMechanismManager
│   └── InteractionVariantController
│       ├── 观察后台任务证据
│       ├── 绑定 primary / temporary / followup
│       ├── 追加 authored 用户请求
│       └── 记录场景机制摘要
│
└── ActiveMetricManager
    └── PersistentTaskMetricRuntime
        └── 普通后台任务指标

InteractionVariantEvaluator
└── 根据已保存的变体证据计算完成结果
```

实现上可以继续复用底层 Provider 证据读取和任务身份绑定代码，但在运行语义、生命周期和输出结构上分开。

## 4. 第一阶段：拆开场景机制和普通指标入口

### 4.1 引入 `ScenarioMechanismManager`

新增一个管理场景机制的组件。它不负责普通指标，只负责安装和推进会改变测试流程的机制。

建议接口如下：

```python
install()
advance(timestamp, turn_boundary)
on_delivery(role, text, event_id, timestamp)
delivery_started(request_id, timestamp)
delivery_completed(request_id, timestamp)
should_extend_reply(timestamp)
consume_reply_step(timestamp)
finalize(termination_reason, timestamp)
close()
```

接口只保留当前编排器实际需要的生命周期方法。普通 `ActiveMetricManager` 继续管理普通 active metrics。

### 4.2 将变体从 `ActiveMetricManager` 的语义中移出

当前变体分支不再追加到 `ActiveMetricManager.metrics`。建议改成：

```python
orchestrator.scenario_mechanism_manager = ...
orchestrator.active_metric_manager = ...
```

两者可以同时存在，但由不同的 manager 负责。

`Orchestrator` 和 `FullDuplexOrchestrator` 在相同的运行节点调用场景机制 manager：

- 每个 tick 或 turn 调用 `advance()`；
- 用户或 Agent 实际交付时转发 delivery 事件；
- 运行结束时调用 `finalize()`；
- 清理时调用 `close()`。

当前 `InteractionVariantRuntime.install()` 会直接向 `ScenarioRuntime` 注册 delivery listener。改成 manager 后，应只保留一个 listener 转发入口，避免 manager 转发和 runtime 自己注册造成重复处理。

### 4.3 `run_simulation()` 直接安装场景机制

不要再使用空的 `active_metrics_config` 表示交互变体。流程改为：

```python
variant = variant_for(orchestrator.task)
if variant is not None:
    scenario_mechanism_manager = build_scenario_mechanism(...)
```

只有用户或配置明确启用普通 active metrics 时，才创建 `ActiveMetricManager`。

这样变体即使没有普通 active metrics 配置，也能正常改变测试流程；普通后台指标也不会因为检测到变体而被隐式创建。

## 5. 第二阶段：重命名变体运行时并调整输出

### 5.1 重命名

将 `InteractionVariantRuntime` 改名为 `InteractionVariantController`，突出它的场景控制职责。

为了兼容历史导入，可以暂时保留：

```python
InteractionVariantRuntime = InteractionVariantController
```

新代码统一使用 `InteractionVariantController`。兼容别名只用于过渡，不作为新接口文档。

### 5.2 分离结果位置

变体运行证据放入：

```python
simulation.info["scenario_mechanisms"]["interaction_variant"]
```

内容包括：

- 变体 ID 和类型；
- 源任务 ID 和源任务版本；
- initial、temporary、followup 请求；
- 请求排队、开始和完成时间；
- primary、temporary、followup 的任务绑定；
- Provider acceptance 事件；
- 任务状态和操作证据；
- `unsupported`、`ambiguous`、`target_unbound` 等运行状态；
- issues 和机制版本。

普通后台任务继续放在：

```python
simulation.info["active_metrics"]["persistent_task"]
```

不要再把交互变体放在 `active_metrics.persistent_task` 下。

建议给场景机制摘要增加独立版本，例如：

```text
interaction-variant-runtime-v2
```

旧结果仍允许读取，不修改历史序列化数据。

## 6. 第三阶段：分离评分逻辑

### 6.1 新增 `InteractionVariantEvaluator`

把当前 `InteractionVariantRuntime.score()` 中的评分逻辑移到独立 evaluator：

```python
class InteractionVariantEvaluator:
    @staticmethod
    def score(summary, communicate_scoring=None):
        ...
```

Controller 只生成事实摘要：

```python
summary = controller.finalize(termination_reason, timestamp)
```

评估阶段再调用：

```python
score = InteractionVariantEvaluator.score(summary, communicate_scoring)
```

评分结果单独保存，例如：

```python
simulation.info["evaluation_log"]["interaction_variant"]
```

第一轮不改变现有分数公式，只改变调用位置和结果归类。

### 6.2 保持现有评分范围

现有 `metric_enabled()` 对 `modify` 和 `cancel` 的普通 environment、execution、state interaction 评分范围仍然有效。

第一轮继续保持：

- 不把交互变体完成分数自动乘入普通 Gaia2 主任务分数；
- 不因为变体存在而给普通环境评分强行填零；
- `not_applicable`、`unsupported`、`target_unbound` 和 Agent 失败继续分开保存；
- `addtask` 保留自己的参考事件和结果规则。

后续如果需要把变体分数纳入主 reward，应单独设计 reward basis，不在本轮混入。

`metric_enabled()` 后续可以改名为更明确的 `variant_measure_applicable()`，但可以先保留兼容函数名。

## 7. 第四阶段：统一用户请求来源

当前变体通过直接操作 `runtime.user_inputs` 注入请求。建议给 `ScenarioRuntime` 增加统一入口：

```python
runtime.queue_user_input(
    request_id,
    text,
    source="interaction_variant",
    purpose="followup",
)
```

来源建议包括：

```text
scenario
interaction_variant
persistent_task
legacy_persistent_task
```

来源只用于轨迹记录和后续分析，不在运行时额外调用裁判模型。

变体 request ID 仍然使用稳定的：

```text
<variant_id>:temporary
<variant_id>:followup
```

这样可以区分请求来源、阶段和用途，而不必依赖用户文本内容。

## 8. 第五阶段：抽取公共证据层

第一轮可以暂时保留 `InteractionVariantController` 对现有后台任务 runtime 的代码复用。稳定后再抽取公共证据组件：

```text
TaskEvidenceSession
├── Provider evidence source
├── observations
├── operations
├── backend task states
├── task identity binding
└── timestamp normalization
```

之后形成：

```text
PersistentTaskMetricRuntime
└── TaskEvidenceSession

InteractionVariantController
└── TaskEvidenceSession
```

公共证据层不负责：

- 生成用户请求；
- 决定阶段推进；
- 计算交互变体分数；
- 修改主任务 reward。

这一步不是第一轮必须完成的工作，避免为了清理继承关系而大规模重写稳定的 Provider 证据逻辑。

## 9. 必须由机制保证的规则

正式运行路径只做轻量、确定性的状态控制：

- 只接受 `gaia2-interaction-variant-v1` 契约；
- 加载变体时检查源任务 ID 和 source hash；
- authored 请求按固定 phase 顺序推进；
- 每个 phase 最多排队和交付一次；
- 初始请求未完整交付时不选择目标任务；
- 后台任务身份不明确时标记 `ambiguous`，不猜测绑定对象；
- 目标任务已终止时记录 `target_finished_before_request`；
- 不使用 LLM 生成变体请求；
- 每条追加请求记录稳定 ID、来源和交付时间；
- 运行事实、机制摘要和评分结果分别保存。

这些规则通过状态机、固定数据契约和对象生命周期实现，不依赖运行结束后的重型审计。

## 10. 不放进正式评测热路径的内容

以下内容放在离线 authoring、发布或专项脚本中：

- 全量任务依赖图审计；
- 全量变体 source hash 校验；
- reference event 完整性检查；
- 全量数据集回放；
- 变体请求自然性和语义人工审核；
- 所有 Provider 的全量兼容性测试；
- 使用额外 LLM 裁判验证每次变体运行。

正式评测只读取已经冻结的变体契约，并执行有限状态机。不要在每次运行中扫描整个 1000 题任务集，也不要因为启用了变体而额外运行完整 replay。

## 11. 轻量验证范围

只增加针对机制边界的单元和小型集成测试：

1. `modify`：初始请求完整交付后，只追加一次 followup。
2. `cancel`：temporary 接受后再追加 followup。
3. `addtask`：运行时副本包含追加请求和参考事件，源任务未被修改。
4. 普通 persistent task：不进入变体 manager 和变体输出路径。
5. 无 task identity 能力：变体状态为 `unsupported`，不注入请求。
6. 多个后台任务竞争同一角色：状态为 `ambiguous`，不猜测绑定。
7. 输出结构：变体位于 `scenario_mechanisms.interaction_variant`，普通指标位于 `active_metrics.persistent_task`。
8. 请求交付：每个 request ID 只进入用户队列一次。

不做以下验收：

- 不运行完整 Gaia2 1000 题集；
- 不依赖真实 Provider API；
- 不调用额外 LLM 做全量判定；
- 不把全量 replay 或数据审计加入 `make test`；
- 不在每次正式评测中执行全量一致性检查。

## 12. 推荐实施顺序

1. 定义 `scenario_mechanisms` 输出结构和版本。
2. 增加 `ScenarioMechanismManager`，接入两个编排器的统一生命周期。
3. 将变体从 `ActiveMetricManager` 的安装和结果集合中移出。
4. 将 `InteractionVariantRuntime` 重命名为 `InteractionVariantController`，保留兼容别名。
5. 调整 `run_simulation()`，不再通过空 active metrics 配置启动变体。
6. 调整结果保存位置和展示读取逻辑。
7. 抽取 `InteractionVariantEvaluator`，保持原有评分公式。
8. 给请求队列增加来源字段和统一入队方法。
9. 增加轻量机制测试和普通 persistent task 回归测试。
10. 在后续独立变更中抽取公共 `TaskEvidenceSession`。

## 13. 完成标准

本计划完成后，应满足：

- 交互变体可以改变测试流程，但不再显示为普通 `persistent_task` 指标；
- 普通 active metrics 未启用时，变体仍能按契约运行；
- 变体运行证据和变体评分分别保存；
- 普通 persistent task 的运行和结果结构保持兼容；
- 变体请求有稳定 ID、来源和交付记录；
- 热路径只执行轻量状态机逻辑，不增加全量审计、全量 replay 或额外 LLM 验收；
- 源任务保持不可变，变体只构造运行时副本。


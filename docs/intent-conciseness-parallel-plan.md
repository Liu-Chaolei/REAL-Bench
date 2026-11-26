# Intent 与 Conciseness 评分优化计划

状态：第一版已实施。Intent repetition 已从新生产路径移除；Intent 两层独立阶段已并行；Conciseness 保持窗口顺序并记录阶段耗时和缓存命中。后续性能优化仍可继续进行。

本计划处理三个目标：

1. 从生产 Intent 评分路径中移除 repetition check；
2. 并行没有数据依赖的 Intent 阶段；
3. 保持 Conciseness 的窗口依赖和评分语义，先做安全优化。

本计划不删除 Gaia2 的 `repetition` task split，也不删除旧 repetition 模块和历史结果读取能力。它只改变新评分流程如何生成 Intent 结果。

## 1. 当前耗时结构

单题 Intent 当前大致经过：

```text
GT extraction
Prediction extraction
Forward evaluation
Backward evaluation
Repetition check
Final score
```

Conciseness 当前按请求窗口处理：

```text
window 0:
  nuggets_0 → alignment_0 → validated_history_0

window 1:
  nuggets_1(history_0) → alignment_1 → validated_history_1

window 2:
  nuggets_2(history_1) → alignment_2
```

Intent 的 GT 和 prediction 提取互相独立。Forward、Backward 和 repetition 在提取结束后也可以独立运行。Conciseness 的后续窗口会读取前一窗口确认过的 `validated_history`，因此不能直接把全部窗口并行化。

## 2. 删除生产 repetition check

`repetition` 是 Intent 评分中的重复委托检查，检查 backend prediction 中多个目标是否是同一个独立请求的重复出现。它与 Gaia2 的 `repetition` task split 是两个概念。

新生产路径删除：

```python
from tau2.diagnostics.intent.repetition import (
    apply_repetition,
    check_repetition,
)
```

以及：

```python
repetition = await check_repetition(...)
verdict = apply_repetition(verdict, repetition)
```

新的 Intent 结果直接使用双向内容评估：

```python
result = score_record(verdict)
```

新结果不再生成：

```text
intent/repetition.json
repetition_adjustments
```

旧模块和旧结果仍然保留：

- `src/tau2/diagnostics/intent/repetition.py` 不立即删除；
- 旧 `tau-real-intent/2` 结果继续可读；
- 历史验证脚本继续使用旧接口；
- 新的生产结果使用新的 Intent 版本，例如 `tau-real-intent/3`。

新旧版本必须分开标识，避免把不带 repetition adjustment 的新结果和旧结果混在一起汇总。

## 3. Intent 并行依赖图

新的依赖图：

```text
                 ┌─ Forward evaluation ─┐
GT extraction ───┤                      ├─ Final score
                 └─ Backward evaluation┘

Prediction extraction ──────────────────┘
```

具体阶段：

```text
第一层：并行 GT extraction 和 Prediction extraction
第二层：并行 Forward evaluation 和 Backward evaluation
最后：本地组合结果并计算 score
```

每个 extraction 内部的 repair 仍保持顺序：

```text
初次生成
  → schema / semantic validation
  → 失败时生成 repair prompt
  → repair request
```

Repair 不能与初次请求并行，因为 repair 依赖上一次的原始输出和校验错误。

## 4. Intent 第一层实现

当前 [call.py](../src/tau2/diagnostics/intent/call.py) 逐个执行 `build_gt()` 和 `extract_prediction()`。改成两个独立 asyncio task：

```python
gt_task = asyncio.create_task(
    build_gt(...)
)
prediction_task = asyncio.create_task(
    extract_prediction(...)
)

gt, prediction = await asyncio.gather(
    gt_task,
    prediction_task,
)
```

两个任务的输入互相隔离：

- GT 只读取用户侧请求；
- Prediction 只读取 backend 输入；
- 两者使用不同的 model profile；
- 两者共享同一个线程安全的 `RequestBudget`；
- 两者使用独立 `TauJsonModel` 实例；
- 两者使用 content-addressed cache，缓存文件不会冲突。

共享预算的规则保持不变：

```text
每个真实 API attempt 消耗一次预算
repair attempt 也消耗一次预算
cache hit 不消耗预算
```

错误处理：

- 单个 extraction 失败时保留错误 artifact；
- 另一个 extraction 已完成的结果仍然写入缓存；
- 最终 Intent 结果标记为 `error` 或 `unavailable`；
- 不新增额外模型检查；
- 不因为并行而增加 `max_requests`。

预期时间从：

```text
GT + Prediction
```

变为：

```text
max(GT, Prediction)
```

## 5. Intent 第二层实现

GT 和 Prediction 都完成后，Forward 和 Backward 评估互相独立：

```python
forward_task = asyncio.create_task(
    check_direction(
        "forward",
        gt,
        prediction,
        ...,
    )
)
backward_task = asyncio.create_task(
    check_direction(
        "backward",
        prediction,
        gt,
        ...,
    )
)

forward, backward = await asyncio.gather(
    forward_task,
    backward_task,
)
```

两个方向需要独立 model 实例，但共享预算。请求缓存 fingerprint 已包含：

```text
direction
prompt
response_schema
profile
validation
```

因此 Forward 和 Backward 不会写同一个缓存文件。

取消 repetition 后，第二层不再创建第三个任务。最终耗时近似：

```text
max(GT extraction, Prediction extraction)
+
max(Forward evaluation, Backward evaluation)
```

本地 `score_record()`、`score_batch()` 和 JSON 写入在所有模型结果完成后顺序执行。

## 6. Intent 结果版本和兼容性

建议定义：

```text
tau-real-intent/2：历史版本，包含 repetition adjustment
tau-real-intent/3：新版本，不执行 repetition check
```

新版本结果：

```json
{
  "version": "tau-real-intent/3",
  "method": "bidirectional_text",
  "forward": {},
  "backward": {},
  "status": "complete"
}
```

旧版本读取时继续走旧结构。新汇总逻辑遇到多个 Intent 版本时分桶统计，不能混合平均。

## 7. Conciseness 的依赖边界

Conciseness 每个 ready window 需要两个阶段：

```text
nugget inventory → reply alignment → delivered_history
```

其中：

- `nuggets` 只看当前窗口的用户需求和执行证据；
- `alignment` 依赖当前窗口的 nugget inventory；
- 后续窗口的输入包含前一窗口的 `delivered_history`。

因此当前评分定义下不能直接执行：

```text
所有 window 的 nuggets 同时运行
所有 window 的 alignment 同时运行
```

这会改变后续窗口收到的 `validated_history`，从而改变评分结果。

当前安全流程保持：

```text
for window in windows:
    nuggets(window, history)
    alignment(window, nuggets)
    history.extend(delivered_history)
```

## 8. Conciseness 先做的安全优化

本阶段不改窗口依赖，只做以下优化：

### 8.1 保留并强化缓存

每个 stage 使用 content-addressed cache：

```text
nuggets cache key:
  prompt + schema + model + inventory version

alignment cache key:
  prompt + schema + model + alignment version
```

缓存命中时不调用模型，也不消耗预算。

### 8.2 阶段计时

为每个窗口记录：

```json
{
  "window_id": "stage:1",
  "nuggets_seconds": 0,
  "alignment_seconds": 0,
  "nuggets_cache_hit": false,
  "alignment_cache_hit": false,
  "status": "measured"
}
```

这用于定位慢请求，不改变评分逻辑。

### 8.3 单窗口内部不并行

同一窗口的 alignment 必须等待 nuggets。失败时只保留当前窗口状态，不调用后续 alignment。

### 8.4 不可评分窗口直接跳过

如果 `build_windows()` 产生：

```text
unavailable
missing delivery
unverified assistant delivery
```

不调用该窗口的模型。当前机制已经支持这一点，需要保留。

## 9. Budget 和并发控制

所有并发阶段共享每题的 `RequestBudget`：

```python
budget = RequestBudget(config.max_requests)
```

`RequestBudget.take()` 使用锁保护，保证并行请求不会超过预算。

建议模型请求按 role 使用独立目录：

```text
requests/
├── gt/
├── prediction/
├── evaluation/
└── conciseness/
```

每个并发任务使用独立 model wrapper。只有预算和只读配置共享。

如果 provider 有并发限制，使用一个评测级 semaphore 限制真实网络请求；本地 JSON 解析和缓存读取不占用 semaphore。

第一版不要引入复杂的全局调度器。使用现有 `asyncio` 和 `RequestBudget` 即可。

## 10. 错误和取消处理

每个并行阶段要保留独立结果：

```text
GT 失败：Prediction 结果仍可缓存，最终 Intent 不完整
Prediction 失败：GT 结果仍可缓存，最终 Intent 不完整
Forward 失败：Backward 结果仍可缓存
Backward 失败：Forward 结果仍可缓存
```

使用 `asyncio.gather()` 时要避免一个异常直接丢弃其他已完成结果。可以让阶段函数捕获异常并返回结构化错误：

```json
{
  "status": "error",
  "reason": "ValidationError",
  "raw_responses": []
}
```

不增加额外 judge，不做自动二次审核。

Conciseness 仍然按窗口顺序执行，窗口内错误只影响当前窗口。

## 11. 跨题并行

已有 `run_saved()` 使用 `ThreadPoolExecutor(max_workers=config.concurrency)` 并行不同 saved runs/trials。保留这层并行。

最终并行层级：

```text
跨题：现有 run_saved concurrency

单题 Intent：
  GT / Prediction 并行
  Forward / Backward 并行

单题 Conciseness：
  窗口顺序
  nuggets → alignment

单题 State interaction：
  保持状态顺序或读取 sidecar
```

不要把所有评分阶段放进一个全局线程池，避免不同题目争抢 provider 并发和预算。

## 12. 轻量验证范围

只验证机制，不做长时间 benchmark 验收：

1. fixture 验证 GT 和 Prediction task 同时启动；
2. fixture 验证 Forward 和 Backward task 同时启动；
3. fixture 验证 extraction repair 仍然串行；
4. 新 Intent 结果不生成 `repetition.json`；
5. 新 Intent 结果可以被 `score_record()` 读取；
6. 旧 Intent v2 结果仍然可以读取；
7. Conciseness fixture 验证窗口顺序不变；
8. 共享 RequestBudget 在并发下不超过上限；
9. cache hit 不产生新的模型请求。

不增加：

- 全 benchmark 运行验收；
- 额外 LLM judge；
- 双版本完整对照；
- 运行时验收器；
- 自动拒绝评测结果的审查系统。

## 13. 实施顺序

### 阶段 1：移除生产 repetition check

- 新生产路径不再调用 `check_repetition()`；
- 新结果使用 Intent v3 版本；
- 旧 repetition 模块和历史读取保留；
- 更新直接依赖 repetition 的生产测试。

### 阶段 2：并行 Intent extraction

- 并行 GT 和 Prediction；
- 保留各自 repair 顺序；
- 保留共享 RequestBudget 和独立缓存。

### 阶段 3：并行 Intent evaluation

- 并行 Forward 和 Backward；
- 统一错误结果和缓存写入；
- 本地组合和 score 计算保持顺序。

### 阶段 4：Conciseness 观测优化

- 增加 nuggets/alignment 阶段计时；
- 记录 cache hit；
- 保持窗口顺序和 validated_history 语义。

### 阶段 5：运行轻量检查

- 运行 diagnostics 单元测试；
- 运行 Intent 并发 fixture；
- 检查旧结果兼容和新结果字段；
- 不运行长时间全量评测。

## 14. 完成标准

```text
生产 Intent 评分不再调用 repetition check
Intent GT / Prediction extraction 可以并行
Intent Forward / Backward evaluation 可以并行
repair、cache、budget 语义保持不变
Conciseness 的 validated_history 语义保持不变
旧 Intent 结果仍可读取
新旧评分版本不会混合汇总
没有新增运行时验收或额外审查模型
```

# 指标精简增量计划：信息覆盖、后台结果命名与工具误用指标

本文记录上一轮指标精简后的新增决定，已按本轮范围实施。上一轮计划见 [metrics-cleanup-plan.md](metrics-cleanup-plan.md)，当前指标清单见 [metrics-inventory.md](metrics-inventory.md)。

本轮核心是：取消当前 Gaia2 整题 COMMUNICATE 评分，统一后台结果字段为 native_result_reward，移除 tool_misuse 及其独立裁判调度。实现依靠调用入口和数据流，不给评测运行增加验收流程。

## 一、确定修改的内容

| 项目 | 修改内容 | 最终状态 |
| --- | --- | --- |
| COMMUNICATE | 从当前 Gaia2 有效评分组件、调用链和能力展示中移除 | 必要信息覆盖交给 required_coverage |
| COMMUNICATE 专用裁判调用 | 取消整题必要信息评分调用 | 不先计算再隐藏 |
| required_coverage | 保留现有定义与计算流程 | 继续属于精简反馈模块 |
| state_result_reward | 新结果不再使用旧名称 | 统一输出 native_result_reward |
| 后台结果汇总 | 改读新字段并兼容历史旧字段 | 新报告只显示 native_result_reward |
| tool_misuse | 移除标记、汇总列和展示 | 工具质量由现有四项执行评分负责 |
| tool_use 裁判 | 停止为独立工具误用指标进行调度 | 不再产生对应模型调用 |
| 配置、输出格式、文档与相关测试 | 同步上述变化 | 防止旧字段在下游重新出现 |

上一轮已取消的 ACTION、NL、Late Rate、Generation quality 等继续保持取消状态，不重复实施，也不恢复。

## 二、保留与暂缓事项

- TICK 版和媒体版语音指标的定义、阈值、计算及汇总方式保持当前状态。
- 保留 selectivity_backchannel、selectivity_vocal_tic、selectivity_non_directed 三项细分指标。
- 现有通话级 selectivity 标记保持当前规则，不新增合成总分。
- 后台任务的操作关联、时机、参数和结果判断保持原样。
- 保留 reply_reward 及后台总分的现有关系。
- 四项执行评分、需求理解、发言有用性、Progressivity、Grounding、精简反馈和 Hidden Ask Rate 保持定义。
- 普通能力均值继续采用同题多次试验先平均、再对题平均。
- 新的整题成功公式与不可评分边界仍留待后续讨论。
- 本轮只统一后台结果命名，不删除固定请求的整个实现。全面切换动态请求另行处理。

## 三、实施步骤

### 1. 在 Gaia2 评分入口取消 COMMUNICATE

主要位置：`src/tau2/evaluator/evaluator.py`。

当前 Gaia2 有独立评分分支，直接在该分支完成：

1. 从有效 reward_basis 中排除 COMMUNICATE。
2. 移除整题 CommEvaluator.calculate_reward() 调用。
3. 新结果不再写入 COMMUNICATE 分数及 communicate_checks。
4. 显式选择旧 COMMUNICATE 评估模式时也不触发整题通信裁判，沿用上一轮 ACTION/NL 的停用处理方式。
5. 日志记录实际评分版本和有效组件。

普通运行与事后重新评分共用这一入口，不额外增加检查层。

任务文件中的历史 communicate_info 暂时保留，其存在不再触发评分。本轮不修改 1,100 道任务内容，不重新生成或封存数据，也不调整其他领域仍然需要的共用评分路径。

### 2. 保留后台回复判断的共用函数

当前后台评分、动态后台评分以及历史后台评分仍复用通信模块的底层函数计算 reply_reward。

明确区分：

```text
整题 COMMUNICATE 评分组件 → 取消
后台 reply_reward 的语义判断 → 保留
```

相关位置：

- `src/tau2/evaluator/persistent_tasks/grader.py`
- `src/tau2/metrics/active/persistent_task/runtime.py`
- `src/tau2/persistent_tasks/legacy/native_grading.py`

只断开整题入口，保留后台需要的底层判断。不为此另建裁判框架或搬动整个通信模块。

通信模型配置仍可能被后台回复判断和其他机制使用，不能随 COMMUNICATE 独立指标一并删除。

### 3. 保留 required_coverage 的现有评价流程

主要位置：`src/tau2/diagnostics/conciseness/` 及当前运行使用的诊断配置。

沿用现有流程：

1. 根据用户要求与执行证据确定应反馈的信息。
2. 判断实际回复覆盖的信息。
3. 计算必要信息覆盖比例。
4. 与长度效率形成精简反馈结果。

required_coverage 属于 conciseness，不是所有运行都默认启用。实施时核对当前使用的配置；需要报告信息覆盖率的配置选择现有 conciseness 流程。不新建替代 COMMUNICATE 的覆盖率裁判，不重复计算。

没有启用该诊断的运行继续按配置执行，不临时补跑。

本轮不把 required_coverage 自动乘入 reward，不增加覆盖率及格阈值，不修改精简反馈公式。整题成功定义后续另定。

### 4. 统一后台结果字段

当前字段位置：

| 实现 | 当前字段 |
| --- | --- |
| evaluator/persistent_tasks/grader.py | state_result_reward |
| persistent_tasks/legacy/native_grading.py | state_result_reward |
| metrics/active/persistent_task/runtime.py | native_result_reward |

实施内容：

1. 两个旧写入位置统一写 native_result_reward。
2. `src/tau2/diagnostics/suite.py` 的汇总名称统一为 native_result_reward。
3. 更新报告和脚本中的读取位置。
4. 读取历史结果时，优先使用新字段；只有新字段不存在才读取 state_result_reward。不能把有效的 0 当成缺失。
5. 新结果只写新字段，不双写。
6. 保留现有操作与结果判断，统一命名不意味着两种实现的判断逻辑被重新设计。

目标结构示例：

```json
{
  "native_result_reward": 1.0,
  "reply_reward": 1.0,
  "reward": 1.0,
  "evaluation_log": {
    "operation_reward": 1.0
  }
}
```

后台公式保持：

```text
persistent_task.reward = native_result_reward × reply_reward
```

### 5. 移除 tool_misuse 与对应裁判调度

主要位置：

- `src/tau2/metrics/call_quality.py`
- `src/tau2/judges/suite.py`
- `src/tau2/scripts/leaderboard/benchmark_quality.py`

按调用链处理：

1. 正常 judge suite 不再调度 tool_use，包括旧配置显式选择它的情况。
2. 不再读取 tool_use 裁判结果生成 tool_misuse。
3. 通话结果结构移除 tool_misuse。
4. 汇总、CLI、报告和相关格式声明移除对应列。
5. 通话质量结果不再依赖工具误用裁判结果。

剩余通话标记只有 selectivity，沿用原规则。三个细分指标仍位于 TICK 面板中。

工具调用日志、参数、执行结果及四项细化评分仍保留。停用独立工具误用评分不影响这些证据和指标。

### 6. 同步输出、缓存和文档

- CLI 与 JSON 报告使用统一字段。
- 同步现有结果格式和受影响的生成 schema。
- 使用已有版本机制更新受影响的评分或输出版本，避免旧格式缓存被当成新结果。
- 不批量重写历史结果文件。
- 更新 metrics-inventory.md：移除 COMMUNICATE、tool_misuse，并统一后台结果名称。
- 明确整题成功公式仍未重新定义。

不新增指标注册平台、发布流程或迁移验收器。历史字段兼容放在已有读取入口中处理。

## 四、轻量验证

所有验证都在开发阶段进行，不加入评测运行链路。

| 检查目标 | 方法 |
| --- | --- |
| Gaia2 不再调用整题通信裁判 | 用 mock 替代裁判，确认对应调用不会发生 |
| 后台 reply_reward 继续工作 | 一个小型后台结果与模拟裁判返回值 |
| 字段统一与旧结果兼容 | 新结果只有 native_result_reward；旧字段可以读取；0 值不丢失 |
| tool_use 不再调度 | 使用调度函数和 mock，不访问模型 |
| selectivity 保持现状 | 使用已有小型阈值用例 |
| 配置与报告可读取 | 小型 JSON 或已有夹具 |
| 文件基本正确 | 改动文件的 Ruff 与差异检查 |

不运行完整题集、不全量回放、不做模型效果验收、不重新审核数据、不调用真实模型。

评测行为通过固定的评分选择、明确的字段写入及现有事件记录实现，不增加“先验收再评分”的运行步骤。

## 五、实施顺序与交付结果

```text
取消整题 COMMUNICATE 调用
→ 保留后台回复判断
→ 统一 native_result_reward
→ 停止 tool_use 调度并移除 tool_misuse
→ 同步输出与文档
→ 少量离线检查
```

完成后：

- 当前 Gaia2 不再产生整题 COMMUNICATE 得分或专用裁判调用。
- 信息反馈覆盖继续由 required_coverage 提供。
- 新后台结果与汇总统一使用 native_result_reward。
- tool_misuse 及其正常运行中的裁判调度退出。
- selectivity 和两套语音指标保持本轮确认的状态。
- 整题综合公式与不可评分边界仍为后续议题。

## 六、实施记录

- Gaia2 入口排除 COMMUNICATE；后台共享的回复语义判断继续使用。
- 当前与 legacy 后台评分统一写入 native_result_reward；汇总优先新字段，兼容旧字段且保留有效的 0/None。
- judge suite 不再调度 tool_use，通话质量只从 ticks 计算 selectivity。
- 通话结果格式更新为 benchmark-quality-v3；评分版本使用现有版本机制递增。
- DeepSeek all 诊断配置加入既有 conciseness 流程；其他未选择该诊断的配置不自动补跑。
- TICK/媒体语音定义、后台结果判断、普通按题等权汇总不变。
- 仅做开发时的小型 mock 用例和文件检查，未添加评测运行验收步骤。
- 验证结果：30 个针对性离线用例通过，改动文件 Ruff、格式、差异检查通过；通话 schema 和信息覆盖配置读取正常。没有调用真实模型，也没有执行全量题集。

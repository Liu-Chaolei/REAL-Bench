# GAIA2 组合意图定义 gaia2

通过 `definition_set: gaia2` 启用。定义作为包资源发布，包括 schema、共享抽取说明、引用校验、计数规则和内容哈希；运行时不依赖 examples。

## 接口

- 业务名称开放；比较符、all/any/not、类型标签及评测状态固定。`constraint.kind` 是业务类别，其他 value.kind 是技术标签。
- 操作输出使用 `goal.outputs[{entity_ref, role}]`。实体声明不使用 created_by_goal，输出适用于新对象和已存在对象，不根据 action 或 role 的词表判断创建。
- 关系使用 `{type, arguments:[{name,value}]}`。目标端点用 goal_ref，多端点用 list；type 和角色名共同解释方向，数组位置不是依赖。
- 量词使用 `{mode,...}`，all/any_one 不带数量；exactly/at_least/at_most 必须有 count；range 必须有 min_count/max_count，含边界。数量为非负整数或 entity_field/trigger_field/undetermined。未知动态数值不求值，已知范围不得倒置。
- entity_field 使用 `{kind,entity_ref,field}` 读取对象、集合或输出的语义属性，不要求字段同应用 JSON 字段同名。trigger_field 仍为本规则局部事件变量。
- 相对日期、state_anchor 和独立时间约束共用 temporal_anchor：user_request、rule_trigger(rule_ref)、goal_completion(goal_ref)、entity_time(entity_ref,field)、absolute_time(value)，真实歧义可用 undetermined。rule_trigger 的目标必须有 trigger。
- absolute_time 保存 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM[:SS]，可带明确 Z/±HH:MM；无时区时不补时区。日内时刻支持 HH:MM 和 HH:MM:SS。基准、活动时间和工具顺序不混同。

## 四环节与计分

GT 在测评前抽取；逐条测评只运行独立后端输入抽取、两次独立原文核对和确定性计分。Qwen 的预测只读取初始用户请求阶段后端实际收到的完整任务消息，不读取 GT 模板或另存的原始 USER 文本。Qwen 接入仍是 post-run sidecar，不向后端注入预测 JSON。

schema/引用/范围/原文片段及未使用 entity 与内联 target 的精确重复检查，共用最多一次格式修复。业务语义由评测模型判定，程序不推断动作同义词。GT 为 generated_draft，semantic_coverage=not_checked。

抽取输出严格使用 `{intent, evidence?}`，不判断任务能否执行或完成。额外字段按schema处理，结构错误最多修复一次，原始响应保留。通过结构校验即为complete，遗漏和错误由双向语义判定计分。

GT 与预测的基础提示只包含抽取规则、共享填写说明和输入，不再重复展示业务 schema。完整业务定义保留在输出 schema 中：json_object 模式由客户端在提示末尾附加一次，json_schema 模式通过 API 参数提供；不支持结构化输出的模型沿用现有文本回退。格式修正也使用同一输出定义。流水线 11.0.0 更新缓存标识，避免复用旧提示的缓存；已有 GT 和历史评分文件仍可独立读取、计分，不会自动重新生成。

evidence.path 只保存，不校验格式或目标是否存在；quote 继续核对原文。

计数策略 gaia2：action、参数、条件、关系、限制等沿用固定语义事实粒度；outputs 每项和 quantifier 整体各计一项。时间锚点随所属事实计分，不展开重复计数。相同实体/selection类型以及相同target/exists选择条件采用精确去重。

评测固定项目边界，但不匹配两份 JSON：forward 提供 GT intent/items 和后端原文，backward 提供 Prediction intent/items 和前端原文。完整单侧 JSON 保留引用、分支和作用范围；同一段原文可以支持多项，不要求双方节点、路径或目标数量一致。任意 ID 不计语义；输出绑定和类型占位只有纯组织含义时可 ignored，实际对象类型与创建、传递关系不能因此丢失。

Recall=正向matched/有效项目数；Precision=反向matched/有效项目数；F1为两者调和平均。ignored 不进入分母；unknown 记 uncertain，该方向及合并F1为null，另一方向的确定分数保留。结果按forward/backward保存独立计数，没有共享TP/FP/FN。方法标记为bidirectional_text；离线score只接受该方法的记录。

## 定义与产物管理

只提供 `definition_set: gaia2` 一套定义。schema、共享说明、校验和计分规则的内容哈希进入记录与缓存，修改规则后不会复用旧缓存。
定义没有版本子目录或其他评分模式。用户原文及实际后端消息保存在记录input中，已有数据不会因整理代码而重新生成。

正式批量结果为 gt.jsonl、prediction.jsonl、evaluation.jsonl 和 scores.json。evaluation.jsonl包含forward/backward判定；scores.json的records包含逐条分数，不另导出重复文件。覆盖报告与断点缓存保留。

确认未向后端提交任务时，生成空预测并跳过预测/判定模型，非空 GT 的 F1=0；后端输入日志缺失或无法可靠关联时 F1=null，不以 ASR 或 objective 摘要替代。

未触发的 reminder/scheduled_task 只有完整 task.scheduled 快照、没有执行迹象时，不要求存在后端消息；来源 metadata 的 excluded_scheduled_tasks 保存任务 ID 和 scheduled_not_triggered 原因。只有排除这些任务后没有应提交的任务且请求文件为零，才确认未提交。完整生命周期中的触发/执行事件仍要求匹配实际消息；实际消息优先于 scheduled 快照。空预测的正向项目全部记 missing，反向没有项目，Precision 按空集合约定为 1；非空 GT 的 Recall=0、F1=0，两边均为空时 F1=1。空预测正常计入评分覆盖率和汇总。

抽取防重：同一对象优先复用 entity_ref。若未被任何业务引用的 entity 与内联 goal.target 的对象类型及完整 selection 一致，轻量检查报告两处路径，并复用已有的一次修复。比较忽略对象键顺序，保留数组顺序与 JSON 类型；不同条件或已使用的 entity 保留。程序不自动改写输出，不扩展到其他重复类型；修复后仍未通过则 error。

## 初始请求评测范围（11.0.0）

Qwen post-run 固定使用 `initial_request`：GT 对应原始 USER 请求，prediction 对应模拟用户第一次实际发送补充音频前的所有初始轮次提交。一个原始请求可以被 VAD 分成多个轮次，任务接受事件也可以晚到；按 `turnId` 归属选择，不按请求文件数或 task.accepted 时间截断。选中消息的正文、final_asr、objective、recent_voice_context 均完整保留。已丢弃的候选回复不构成边界，部分发送的补充语音构成边界。

通用 backend source 保留默认 `scope: all_submissions`，离线复现新范围需设置：

```yaml
prediction_source:
  format: backend
  path: backend
  gateway_path: gateway.jsonl
  observation_path: observation.json
  scope: initial_request
  record_id: your-request-id
```

来源元数据保存观察文件哈希、边界时间、选中/排除的任务及消息来源。模型只收到选中的实际后台文本和范围说明，不接收 observation 内容、模拟器内部状态或隐藏 GT。证据完整且初始阶段没有提交时按空预测评分；缺少观察文件、输入未完成、轮次归属不明时明确标记 unavailable，不自动退回全对话。

官方通过/未通过与四项评分继续覆盖完整执行过程。历史成绩不自动覆盖，旧范围缓存不会用于新范围。

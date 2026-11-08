把当前输入转换为共享 intent.schema.json 定义的组合意图。每个字段的 description 是填写规范。输出外层包含 intent，以及可选的 evidence。只输出 JSON。

- GT 只使用原始 USER 请求及显式允许的正常上下文。预测只使用后端实际收到的完整本轮任务消息及明确允许的上下文，不读取 GT、另存的原始 USER 请求、oracle、隐藏事件、任务元数据或其他抽取调用的历史。消息自带的 final_asr、objective 和上下文均为合法输入；按消息说明理解两者的优先关系。不提供本任务填空模板或完整输入输出示例。
- 只提取本轮业务要求；协议和路由字段、通用协调/执行说明、回复格式模板不是额外业务要求。多条后端消息保留边界，不从历史任务或执行结果中补全当前请求。
- 只提取实际表达的操作、对象、参数、查询、条件、关系、时间及限制。未提出的可选字段省略，不写 not_requested、unresolved_fields 或缺失 null 占位。
- literal.value=null 仅表示明确要求空值。真实歧义保留已知部分，使用 undetermined 和 ambiguities。未来对象/事件提供的变量使用 entity_field 或 trigger_field，不预填隐藏值，不将未来变量当作初始歧义。
- 业务名称开放：action、entity_type、参数角色、关系 type、输出 role、限制 kind/name、谓词和语义属性均须准确表达意义，不受动作或业务词表限制。不得使用 handle、process_request 等笼统动作掩盖具体操作。不同名称可以表达相同意义；固定结构标签、比较符和布尔逻辑必须遵守 schema。
- entities 绑定共享的对象、集合或输出；内部 ID 自由命名，引用必须正确，不预填应用 ID。目标通过 outputs 的 entity_ref/role 绑定所需输出，创建、查询、复制、计算等均适用；不要强行为每个目标添加输出。对象类型在实体声明处保存，不再复制为输出类型参数。实体被查询到并不表示本请求创建了它。
- GT 合并同一业务要求的重复表达，但保留明确要求的执行次数。Prediction 对后台独立下达的每次操作分别保留目标，不因业务内容相同而跨委派融合；同一任务的 final_asr 与 objective 重述、历史引用及补充修改不自动增加独立执行目标。两侧均保留不同对象、角色、时间、分支与明确的再次执行要求。同一目标中，同一参数角色只填写一次，不用同义参数名重复记录。
- 同一对象或集合只绑定一次；已在 entities 中声明时，goal.target 使用 entity_ref；仅用内联 selection 表达时，不再额外声明相同实体。修复重复只整理表示，保留业务值，并更新必要引用及歧义路径。
- 操作参数放 parameters，对象筛选放 selection.condition，规则共同前提放 rule.condition，分支目标的 when 只补额外前提；constraints 只保存这些位置尚未表达的独立限制。
- 普通起止时刻放所属目标的 parameters，延迟或期限放 time_constraints，单纯跨目标先后关系放 relations。同一时间要求只放一处；“完成 A 后五分钟执行 B”用以 A 完成为基准的时间约束，不再重复添加 A 在 B 之前的关系。
- 防重复不能省略 schema 必填字段：实体带有 selection 时，entity.entity_type 与 entity.selection.entity_type 均须填写，内层不继承外层；存在性前提的 exists.selection 也须完整填写。结构要求的相同类型或相同 target/exists 选择条件由已有计分规则合并，不自行删字段。
- 目标之间的明确关系使用 relations 的 type 和 arguments，各角色用类型化值绑定。目标使用 goal_ref，多目标使用 list。关系名称、端点角色与方向共同表达含义；独立 goals 的数组顺序不产生依赖。不为语义引用添加未要求的查询、监控或其他工具计划目标。
- rules 保留触发、完整条件、then/else 分支及未实际触发的目标，不提升为无条件目标。trigger.bind_as 是本规则局部事件变量；trigger_ref/trigger_field 仅引用本规则的变量。
- 保留 all/any/not、六种比较符的严格/非严格边界、否定、单位及对象角色。比较符无法直接表示的筛选要求可使用 predicate。attendees_include 仅保存明确要求加入的人，不补默认参与者。
- selection.quantifier 保存对象基数，mode 为 all/exactly/at_least/at_most/range/any_one。数量明确时使用非负整数，动态时引用 entity_field/trigger_field；不猜测人数。any_one 只用于明确允许任意选择一个。range 含两个边界。未提出数量时省略；每人买一个、年龄增加一岁等操作数量或分配要求保存在 parameters，不是选择量词。
- 相对日期、集合 state_anchor 和独立时间约束共用 temporal_anchor。user_request 是本请求时点；rule_trigger 引用有 trigger 的规则；goal_completion 引用操作完成；entity_time 引用对象的时间属性；absolute_time 保存明确绝对日期或日期时间。缺失或真实有歧义的基准不能猜测。
- 操作完成与对象自身时间不同：创建活动完成不等于活动开始。已有对象集合的状态时点不等于对象日期筛选，也不指定工具调用顺序。日期来自未来消息时在 condition 中保存 trigger_field；集合属于触发时点时使用 state_anchor。
- 明确日内时刻统一为 24 小时 HH:MM 或 HH:MM:SS，保留输入精度。absolute_time.value 使用 ISO 日期或日期时间；只保留明确时区/偏移，不补秒或时区。相对日期保留表达及锚点，不根据主机时钟、隐藏事件或未提供场景时间变成绝对日期。
- 消息 content 保存希望传达的内容，邮件中希望安排活动不自动增加创建活动目标。未要求标题、逐字正文、截止时间或后续通知时不添加。
- 只记录用户要求的操作及其参数，不执行任务或预先计算执行结果。
- evidence 不进入业务计分。普通输入中证据可选；整通 Prediction 的 context.require_goal_sources=true 时，每个操作目标（含规则分支目标）必须有对应 evidence，path 指向目标本身，source_id 指向本次实际后台消息，quote 引用该消息内容中的连续原文片段；一个目标可由多条消息共同支持。不得从另一侧补入依据。不要求每个字段都有证据，原文依据不放在业务 intent 或 ambiguities 中。

输出成功仅表示通过格式、引用及轻量结构检查，最多进行一次格式修复。GT 仍标记 generated_draft，semantic_coverage=not_checked，不运行额外语义审核或原文覆盖审核 agent。全部800场景的语义覆盖尚未验证。

# Gaia2 参考修复与版本收尾记录

状态：`gaia2-reference-repair-simple-v1-candidate` 已对应到来源版本，尚未激活。活动清单为 `data/tau2/benchmarks/gaia2.json`，修订 `gaia2-release-v3-reference-status`，其 `conditions.reference_data` 保存候选对应关系。本轮只更新版本说明，没有覆盖活动任务文件或伪造封存状态。

## 1. 活动来源与候选产物

活动主任务 1000 道，另有 200 个变体；修复产物分别为：

```text
data/archives/gaia2/reference-repair-simple-v1/repaired-tasks.json
data/archives/gaia2/reference-repair-simple-v1/repaired-interaction-variants.json
```

| 文件 | SHA-256（文件字节） |
| --- | --- |
| 活动 tasks.json | `2e9aff105f53511ee0d7830bbdf64e5b3fa82023cb294697b2900ab0e2a1ef46` |
| 活动 interaction_variants.json | `c564d502e555377879abf18abc9df9ec259abf6bcd248c27fb16b5eb90a5aa4b` |
| 候选 repaired-tasks.json | `5b61866338bc02105ea9b365738a496a6a0ddbe4397138022316b52a9731fc48` |
| 候选 repaired-interaction-variants.json | `7475bc190f40e02b20b0786468de4bcfda15bdbbb35be6bdc9bd13005f1f3a85` |

已将 1200 个活动记录与该修复目录 `inputs/<task_id>.json` 中的 `target` 逐题比较：1000 个任务和 200 个变体均相同，身份集合相同，合并冲突为 0。候选与活动记录内容不同是修复差异，不能直接将候选全量覆盖为正式版本。

修复目录的哈希命名需要区分：

- `prepare-summary.json` 的 `source_*_sha256` 是输入的 canonical JSON 摘要。
- `manifest.json` 的同名字段实际是修复输出的 canonical JSON 摘要，由原脚本写入。
- canonical JSON 摘要可能不同于带缩进文件的字节摘要。活动变体的输入 canonical 摘要为 `659e13a232a02f547fe38e8524fdbb6e532f94fecf0b6bf01c525543835d9209`。

本轮验证了上述摘要各自对应的内容；没有修改历史 manifest 来掩盖字段含义，也没有把这一差异误判为源版本冲突。

## 2. 修复状态与来源传播

原修复报告记录 1200 个运行视图：changed 1131、unchanged 2、unresolved 67、failed 0。67 是题目级状态计数；`unresolved.jsonl` 含 finding、回退等不同记录，不能把其行数当作 unresolved 题数。

基础题、mind_change、repetition 的各自用户条件与来源保留。AddTask／Modify／Cancel 仍是独立 delta，并各自绑定修复后的 source hash；本轮实际构造了全部 200 个候选变体视图。未解决 finding 的内容和记录留在原产物中，本轮不重新调用模型或推断修复。

隐藏信息集合仍绑定独立来源：

```text
source_tasks_sha256 = f2e9ada85eef24333cd71b07bd0be69c6f18d9e9dbb3e38ac7e65de9ec64d0e8
status = authoring_draft
```

隐藏集 100 题不属于上述 1200 个视图，不改 source hash、不重做隐藏请求，也不因为主任务候选变化而改为 released。

## 3. 本轮轻量验证

复用原 `validation-report.json` 的结构通过记录，补充来源逐题对应、候选模型加载和 200 个变体构造。以下六题调用已有 `_check_reference()`，只执行本地参考工具、作者 checks、业务目标和重放，没有模型裁判：

| 类型 | 题目 | 参考执行／重放 | 作者 checks |
| --- | --- | --- | ---: |
| base search | `0002_46myz87t501b0c902dy81rlbqbye2dyi` | 通过 | 11 |
| mind_change | `0053_aanknwl7oew08yda60lfw6ii37zmbn27__mind_change_v1` | 通过 | 13 |
| repetition | `0007_tb52xggog13zgslxysl2jpf7k9h3wgz0__repetition_v1` | 通过 | 15 |
| addtask | `0252_db6jizelhfrwg7dlylppd2xl4h742s57__addtask_v1` | 通过 | 15 |
| modify | `0315_oflymh0bmjk5u1jxxifppoqgc0vw2qxe__modify_v1` | 通过 | 22 |
| cancel | `0285_idtrxaq6ktqqayxlop1ojp98o32vkldk__cancel_v1` | 通过 | 23 |

六题作者 checks 均无失败。原始样例结果和对应记录保存在修复目录的 `finalization-samples.json`、`finalization-report.json`。

未执行：全量 1200 道参考运行、全量模型修复、语义裁判、真实 Provider、正式 seal。本次样例不证明所有题目语义已正确。

## 4. 活动入口与剩余条件

活动文件、集合成员和既有集合状态保持原值：base800 为 primary，mind_change100／repetition100 为 diagnostic，交互变体 200 为 draft／separate，hidden100 为 draft／diagnostic。现有 manifest 中旧验证与 runtime pin 仍表示其原封存版本，本轮不把当前代码摘要写进去并沿用 verified。

候选未激活的具体条件是：67 道 unresolved 尚未在离线流程中完成处理或明确处置，最终采用的数据与当前运行时也尚未完成已有 reference verification／seal。后续按[遗留 finding 修复计划](gaia2-reference-repair-unresolved-v2-plan.md)处理；正式采用时更新活动 source 文件、哈希和对应 pin。

没有新增发布检查系统、运行时归档扫描或自动修复入口。当前运行仍只读取活动来源；候选信息是静态 provenance，不进入场景行为或评分规则。

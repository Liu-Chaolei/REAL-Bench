# Gaia2-only multilingual 清理与迁移计划

状态：已完成 Gaia2 语言运行层、CLI/registry 和旧数据迁移；实现说明见 [Gaia2 English runtime](gaia2-language-runtime.md)。历史材料保留在本地归档。

本文规划如何把剩余的 multilingual 代码收缩成 Gaia2 所需的最小语言运行能力。目标是让 Gaia2 独立完成文本和语音评测，同时移除 τ 的多语言发布、区域化和 authoring 体系。

本计划不修改 reward、metrics、evaluator 或评分规则。数据完整性和路径检查只放在 loader、manifest、registry 和运行配置边界中，不进入正式评测流程。

## 一、当前状态

当前已经完成：

- data/tau2/multilingual/ 的活动数据已移除；
- data/tau2/multilingual_extensions/ 的活动扩展已移除；
- Gaia2 使用 data/tau2/domains/gaia2/language/en/；
- Gaia2 可以加载 text 和 voice context；
- Gaia2 不再通过 τ English pack 加载；
- 旧 task_subsets 和 τ benchmark manifests 已移除；
- 新结果统一写入 data/results/。

当前仍然存在：

- src/tau2/multilingual/runtime.py；
- src/tau2/multilingual/regional.py；
- src/tau2/multilingual/scenario_dispatch.py；
- src/tau2/multilingual/localization.py；
- src/tau2/multilingual/cli.py；
- 地址、机场、姓名和区域化辅助模块；
- 残留的 factory CLI 文件；
- runner 对 tau2.multilingual.runtime 的引用。

当前行为是：

~~~text
load_language("en", "gaia2") → 正常
load_pack("en")              → 失败，旧 τ pack 已不存在
list_languages()             → 空列表
~~~

这说明 Gaia2 路径已经可用，但旧 multilingual API 还没有完成代码层清理。

## 二、目标结构

最终保留的语言数据：

~~~text
data/tau2/domains/gaia2/
└── language/
    └── en/
        ├── pack.yaml
        └── metadata.json
~~~

最终保留的 Gaia2 语言代码：

~~~text
src/tau2/domains/gaia2/
├── language.py
├── language_runtime.py
└── manifest.py
~~~

通用运行数据模型继续保留：

~~~text
src/tau2/data_model/language.py
~~~

其中保留：

- LanguageTaskInfo；
- TaskMetadata；
- LanguageArtifactPins；
- 运行结果中的语言上下文类型。

## 三、重新定义 Gaia2 language pack

当前 data/tau2/domains/gaia2/language/en/pack.yaml 可以继续使用现有 schema，但内容需要重新整理。

pack 只允许绑定 Gaia2：

~~~yaml
language: en
revision: gaia2-language-v2
domains:
  gaia2:
    source: registry
~~~

pack 中保留：

- Gaia2 用户模拟规则；
- 已释放信息和未来信息的可见范围；
- 只能根据实际听到内容回答的规则；
- Gaia2 text caller profile；
- Gaia2 voice caller profile；
- Gaia2 任务 metadata 绑定；
- English 语言信息。

pack 中删除：

- airline、retail、telecom、banking、intake 的 domain binding；
- regional bundle；
- pack extension；
- 旧 τ 多语言术语；
- 区域数据库、本地货币、机场和姓名规则；
- 旧 τ 客服场景专用提示。

当前 pack 中的 mamadou_diallo、priya_patil 等 persona 来自旧 τ English pack，需要重新命名和重写。可以保留 PackPersona 的字段结构，但必须重写：

- persona 名称；
- caller 背景；
- 确认、澄清、等待和拒绝行为；
- 语气和语用规则；
- 语音打断与 backchannel 行为；
- voice ID；
- voice examples。

## 四、建立 Gaia2 language runtime

新增：

~~~text
src/tau2/domains/gaia2/language.py
src/tau2/domains/gaia2/language_runtime.py
~~~

language.py 负责：

- 固定读取 Gaia2 pack；
- 固定读取 Gaia2 metadata；
- 校验语言为 en；
- 校验 domain 为 gaia2；
- 返回已验证的 Gaia2 pack；
- 计算 pack 和 metadata digest。

language_runtime.py 从现有 src/tau2/multilingual/runtime.py 中迁移：

- LoadedLanguage 中与 Gaia2 相关的部分；
- Gaia2 task loader；
- metadata 映射；
- persona 选择；
- resolve_task；
- task_context；
- agent_language_guidance；
- language_review_user_info；
- pin_language_artifacts；
- task_digest；
- text/voice context 构造。

Gaia2 runtime 不包含：

- regional_bundle_file；
- localize_db；
- pack_extension；
- canonical regional scenario；
- banking/intake 特殊分支；
- 多语言数据库替换；
- 语言 × domain 矩阵逻辑。

## 五、runner 依赖迁移

逐步修改：

- src/tau2/runner/build.py；
- src/tau2/runner/batch.py；
- src/tau2/runner/helpers.py；
- src/tau2/runner/simulation.py；
- src/tau2/runner/checkpoint.py；
- src/tau2/metrics/benchmark.py；
- src/tau2/domains/gaia2/recorded.py；
- src/tau2/gym/gym_agent.py。

将：

~~~python
from tau2.multilingual.runtime import ...
~~~

改为：

~~~python
from tau2.domains.gaia2.language_runtime import ...
~~~

Gaia2 运行配置暂时保留：

- language；
- language_artifacts；
- language_context；
- pack_revision；
- task_sha256。

新 Gaia2 运行不再设置：

- pack_extension；
- localize_db；
- regional_bundle_file；
- domain localization revision。

旧字段可以先保留在模型中，用于读取历史结果；等历史读取入口完成迁移后再删除。

recorded.py 的归档格式统一为：

~~~text
<archive>/
├── tasks.json
├── manifest.json
├── language/
│   └── en/
│       ├── pack.yaml
│       └── metadata.json
└── archive.json
~~~

不再生成 pack.yaml、language_extension 或 multilingual_extensions。

## 六、CLI 收缩

Gaia2 对外提供：

~~~bash
tau2 run --benchmark gaia2
tau2 run --benchmark gaia2 --audio-native
tau2 benchmarks list
tau2 view --dir data/results
~~~

删除或停用：

- tau2 languages；
- tau2 factory；
- tau2 intake-tasks；
- 多语言 pack 选择；
- --no-localize-db；
- --pack-extension；
- 多语言 benchmark filter；
- 语言矩阵和区域 domain filter。

过渡阶段可以保留 --language en。当所有 runner 和结果读取逻辑完成迁移后，再删除该参数。

当前 src/tau2/multilingual/cli.py 中的 communicate_scoring_from_args 如果仍然需要，应迁移到：

~~~text
src/tau2/evaluator/semantic_communication_cli.py
~~~

communicate scorer 参数属于语义通信配置，不应继续依附 multilingual 命名空间。

## 七、registry 收缩

Gaia2-only 阶段，registry 最终只保留：

~~~text
gaia2
gaia2_hidden
~~~

旧 domain 的注册最终移除：

- airline；
- retail；
- telecom；
- banking_knowledge；
- intake；
- mock。

顺序：

1. 完成 Gaia2 runner 迁移；
2. 确认 manifest 只选择 Gaia2；
3. 确认旧 domain 没有默认入口；
4. 删除 registry 注册；
5. 再删除旧 domain 源码和数据。

## 八、删除旧 multilingual 源码

确认所有活动 import 已迁移后，删除：

~~~text
src/tau2/multilingual/address_format.py
src/tau2/multilingual/airport_catalog.py
src/tau2/multilingual/caller_identity.py
src/tau2/multilingual/localization.py
src/tau2/multilingual/names.py
src/tau2/multilingual/prompt_review.py
src/tau2/multilingual/regional.py
src/tau2/multilingual/scenario_dispatch.py
src/tau2/multilingual/cli.py
src/tau2/multilingual/factory/
~~~

当前 factory/cli.py 引用的其他 factory 模块已经不存在。它不再是可用命令，应删除或改为明确的弃用 stub。

删除前需要先迁移：

- communicate_scoring_from_args；
- 通用语言上下文类型；
- Gaia2 runtime 使用的 prompt rendering；
- 结果读取和 archive 验证逻辑。

## 九、删除旧 domain 数据

在 registry 和 runner 完成迁移后，再处理：

~~~text
data/tau2/domains/airline/
data/tau2/domains/retail/
data/tau2/domains/telecom/
data/tau2/domains/banking_knowledge/
data/tau2/domains/intake/
data/tau2/domains/mock/
~~~

这些目录可以先移动到：

~~~text
data/archives/tau-legacy-domains/
~~~

确认不再被代码读取后，再决定是否永久删除。

保留：

~~~text
data/tau2/domains/gaia2/
data/tau2/domains/gaia2_hidden/
data/archives/
data/results/
data/voice/
~~~

历史 data/simulations/ 和 data/tau2/results/final/ 暂时作为只读材料处理。

## 十、文档和测试清理

删除或重写：

- τ language pack 文档；
- tau2 factory 文档；
- regional localization 文档；
- task subset 文档；
- τ multilingual benchmark 文档；
- 旧语言矩阵测试；
- factory 测试；
- 旧 pack fixtures；
- 旧多语言发布示例。

新增 Gaia2 文档，说明：

- Gaia2 pack 字段；
- English-only 约束；
- text/voice 运行模式；
- task group 和 manifest；
- data/results 输出结构；
- 语言 revision 和 artifact pin；
- 动态异步作为运行能力，而不是固定任务集。

## 十一、机制约束

清理规则只放在机制边界。

Gaia2 loader：

- 固定读取 Gaia2 pack；
- pack 不存在时停止；
- language 不是 en 时停止；
- domain 不是 gaia2 时停止；
- metadata 不覆盖任务 ID 时停止。

Gaia2 manifest：

- 校验源文件 hash；
- 校验任务 ID；
- 校验任务组数量；
- 区分 released、draft 和 diagnostic；
- 保留基础任务、交互变体和 hidden intent 的组信息。

registry：

- 只注册 Gaia2；
- 旧 domain 不再进入正式 runner；
- 不让已安装目录自动扩展 benchmark。

结果路径：

- 相对路径统一写入 data/results；
- 旧 data/simulations 只接受显式路径；
- 结果中记录 pack revision、manifest revision 和 task digest。

这些检查只保证运行条件一致，不参与 reward 或 evaluator。

## 十二、轻量验证

只做：

1. 导入 Gaia2 language runtime；
2. 加载 Gaia2 pack；
3. 构造一个 text context；
4. 构造一个 voice context；
5. 读取 Gaia2 manifest；
6. 加载一个 base task group；
7. 执行一次 tau2 run --benchmark gaia2 --dry-run --num-tasks 1；
8. 确认输出路径为 data/results；
9. 搜索活动代码中是否还有 tau2.multilingual.runtime import。

不做：

- 全量 1000/1200 道任务；
- 参考执行重跑；
- 全量语音运行；
- 全量 metrics；
- 多语言矩阵；
- API 评测；
- 新增 reward 或诊断指标。

## 十三、执行顺序

~~~text
重写 Gaia2 persona
→ 创建 Gaia2 language.py 和 language_runtime.py
→ runner 脱离 multilingual.runtime
→ 迁移沟通评分 CLI
→ CLI 收缩为 Gaia2
→ registry 只保留 Gaia2
→ 删除 regional/localization/factory 源码
→ 删除旧 domain 数据和 registry
→ 更新文档和测试
→ 执行少量 dry-run 机制检查
~~~

每一步先完成新的加载路径，再删除旧路径，避免 Gaia2 运行链路被提前切断。


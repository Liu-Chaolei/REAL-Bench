# τ-bench 数据整理与 Gaia2 评测迁移计划

状态：已完成 Gaia2 语言运行层、CLI/registry 和旧数据迁移；实现说明见 [Gaia2 English runtime](gaia2-language-runtime.md)。历史材料保留在本地归档。

本文定义当前项目从 τ-bench 原有数据迁移到 Gaia2 评测的整理方案。目标是让 Gaia2 的任务、发布清单和运行结果独立工作，同时移除不再使用的 τ 数据。

本计划只涉及数据组织、加载机制、运行输出位置和必要的代码边界调整。不会把数据整理规则写入任务 reward、metrics 或其他评测逻辑，也不会通过大规模评测来完成验收。

## 一、最终目标

目标目录结构：

```text
data/
├── results/                         # 新运行结果
├── tau2/
│   ├── domains/
│   │   ├── gaia2/                   # Gaia2 主任务和运行资源
│   │   └── gaia2_hidden/            # 隐藏信息任务
│   └── benchmarks/
│       └── gaia2.json               # Gaia2 发布清单
├── archives/                        # 历史制作、审核和修复材料
└── voice/                           # 仍需使用的公共语音资源
```

旧的 `data/simulations/` 暂时保留为历史只读目录。新运行统一写入 `data/results/`，不立即搬运或删除旧结果。

需要完成的四项变化：

1. 删除 `task_subsets` 及其运行依赖。
2. 删除 τ 专用 `multilingual` 数据，保留并拆出 Gaia2 需要的最小语言运行能力。
3. 删除旧 τ benchmark manifests，建立 Gaia2 发布清单。
4. 将新运行的结果、metrics、日志、音频和诊断材料统一写到 `data/results/`。

## 二、数据边界

### 2.1 保留的数据

保留：

- `data/tau2/domains/gaia2/`；
- `data/tau2/domains/gaia2_hidden/`；
- Gaia2 的初始状态快照和附件资源；
- Gaia2 动态后台任务的公共模板；
- `data/voice/` 中仍由语音运行使用的背景噪声；
- `data/archives/` 中用于历史追溯的制作和审核材料。

### 2.2 删除的数据

删除：

```text
data/tau2/task_subsets/
data/tau2/benchmarks/text.json
data/tau2/benchmarks/voice.json
data/tau2/benchmarks/voice-v1.json
data/tau2/benchmarks/banking.json
data/tau2/benchmarks/entity.json
```

这些文件服务于 τ 原有的语言 × 领域基准和固定抽样，不属于 Gaia2 评测。

在确认 Gaia2 已经脱离 τ English pack 后，再删除：

```text
data/tau2/multilingual/
data/tau2/multilingual_extensions/banking/
data/tau2/multilingual_extensions/banking-dates-v1/
data/tau2/multilingual_extensions/intake/
```

`data/tau2/multilingual_extensions/gaia2/en/` 不直接保留原状。它需要先迁移为 Gaia2 独立配置，因为当前文件中的 `base_pack_sha256` 仍绑定 τ 的 `multilingual/en/pack.yaml`。

### 2.3 暂不删除的数据

暂不删除：

- `data/simulations/`；
- `data/tau2/results/final/`；
- `data/archives/`；
- 旧的 Gaia2 参考验证和审核材料。

它们可能包含历史评测证据、参考答案修复记录或审计材料。后续单独分类处理，不与活动任务数据一起删除。

## 三、删除 task_subsets

### 3.1 原有作用

`data/tau2/task_subsets/` 保存 τ 领域的固定抽样 ID，例如：

- `airline_50`；
- `retail_50`；
- `telecom_50`；
- `retail_114`；
- `telecom_114`；
- `banking_knowledge_97`；
- `intake_200`。

这些文件不保存新的任务内容，只保存从完整任务集选出的任务 ID、抽样种子和摘要。

Gaia2 已有明确的任务类别、任务 ID、来源哈希和变体关系，不需要通过这些抽样文件选择任务。

### 3.2 代码改动

当前 benchmark 模型和 runner 强制依赖 subset，需要修改：

- `src/tau2/data_model/benchmark.py`；
- `src/tau2/runner/presets.py`；
- `src/tau2/task_subsets/`；
- `src/tau2/cli.py`；
- 相关 benchmark 测试和文档。

删除或停用：

```text
tau2 tasks list
tau2 tasks show
tau2 tasks verify
tau2 tasks subset
```

以后任务选择直接来自 Gaia2 manifest 中的任务组和任务 ID。

## 四、multilingual 的数据和代码拆分

### 4.1 data 部分

当前 `data/tau2/multilingual/` 不是通用翻译表，而是 τ 的多语言客服评测数据。它包含：

- 客服用户 persona；
- 性别、地区、语言和语气；
- 开场问候、确认、拒绝、催促和投诉方式；
- 语音停顿、填充词、backchannel 和打断行为；
- airline、retail、telecom 的领域术语和任务语言规则；
- 日期、金额、地址、电话、姓名和机场的本地化表达；
- banking 和 intake 的语言扩展。

这些内容是 τ 的客服基准设计，不作为 Gaia2 的评测规则保留。

### 4.2 Gaia2 独立语言配置

在 Gaia2 目录中建立最小独立配置：

```text
data/tau2/domains/gaia2/language/
├── pack.yaml
└── metadata.json
```

只保留：

- Gaia2 用户模拟规则；
- 已释放信息和未来信息的可见范围；
- 只能根据实际听到内容回答的规则；
- 任务 ID 与元数据映射；
- English 运行所需的语言字段。

不迁移 τ 的客服 persona、领域术语、区域数据库本地化和多语言 voice 规则。

### 4.3 src 部分

`src/tau2/multilingual/` 当前混合了通用运行能力和 τ 专用代码。

应保留或迁移的通用能力：

- `LanguageTaskInfo`；
- 运行中的语言和任务上下文；
- 运行记录中的语言信息；
- artifact pin；
- agent/user guideline 的最小接口。

应逐步移除的 τ 专用能力：

- 多语言 pack 加载；
- regional localization；
- airline、retail、telecom 的区域构建；
- banking/intake 扩展；
- multilingual factory；
- 多语言发布和迁移 CLI。

建议新增通用运行层：

```text
src/tau2/language_runtime/
├── context.py
├── loader.py
└── guidance.py
```

Gaia2 自己负责 pack 加载：

```text
src/tau2/domains/gaia2/language.py
```

迁移期间可以保留 `src/tau2/multilingual/runtime.py` 作为兼容转发层，内部转到新运行层。待所有调用迁移后，再删除 τ 专用 multilingual 代码。

重点修改文件：

- `src/tau2/domains/gaia2/recorded.py`；
- `src/tau2/domains/gaia2/utils.py`；
- `src/tau2/runner/build.py`；
- `src/tau2/runner/batch.py`；
- `src/tau2/runner/helpers.py`；
- `src/tau2/runner/simulation.py`；
- `src/tau2/registry.py`。

其中 `recorded.py` 当前会复制 `multilingual/en/pack.yaml`，必须改为读取 Gaia2 自己的 pack。

## 五、Gaia2 benchmark manifest

### 5.1 新清单

新增：

```text
data/tau2/benchmarks/gaia2.json
```

不继续使用当前 τ manifest 的 `task_subsets + cells` 结构。Gaia2 不是多语言矩阵，也不应依赖固定抽样目录。

### 5.2 任务组

清单至少包含：

| 任务组 | 数量 | 当前定位 |
|---|---:|---|
| `base_search` | 160 | 基础任务，主评测候选 |
| `base_execution` | 160 | 基础任务，主评测候选 |
| `base_ambiguity` | 160 | 基础任务，主评测候选 |
| `base_adaptability` | 160 | 基础任务，主评测候选 |
| `base_time` | 160 | 基础任务，主评测候选 |
| `mind_change` | 100 | 改口条件 |
| `repetition` | 100 | 重复表达条件 |
| `addtask` | 100 | 追加任务变体 |
| `modify` | 50 | 修改任务变体 |
| `cancel` | 50 | 取消任务变体 |
| `hidden_intent` | 100 | 隐藏信息条件 |

`addtask`、`modify`、`cancel` 当前仍是生成草稿，应在清单中记录其状态，不默认和基础任务混合计算。

### 5.3 清单内容

每个任务组记录：

- 来源文件；
- 任务 ID；
- 数量；
- 任务内容哈希；
- 快照哈希；
- 附件资源哈希；
- 评分模式；
- 发布状态；
- 是否属于主评测、诊断或独立实验。

动态异步没有固定题号，应作为运行能力或条件记录，不能伪装成固定任务集。

### 5.4 Gaia2 manifest loader

新增：

```text
src/tau2/domains/gaia2/manifest.py
```

负责：

1. 读取 `gaia2.json`；
2. 校验任务源文件哈希；
3. 根据任务组解析任务 ID；
4. 检查任务 ID 是否存在；
5. 加载主任务、交互变体和隐藏任务；
6. 返回任务列表及任务组元数据。

加载器只做机制级的快速保护：文件缺失、哈希错误、ID 不存在或数量不一致时立即停止。

它不运行模型、不执行参考任务、不计算 reward，也不参与 metrics。

## 六、结果目录迁移

### 6.1 新路径

新建：

```text
data/results/
```

相对形式的 `--save-to` 统一解析为：

```text
data/results/<run_name>/
```

绝对路径仍然允许直接使用。

### 6.2 目录结构

第一阶段只迁移顶层根目录，保留当前内部格式：

```text
data/results/<run_name>/
├── results.json
├── simulations/
├── artifacts/
├── metrics/
├── judges/
├── logs/
└── run-manifest.json
```

需要统一修改：

- `src/tau2/config.py`；
- `src/tau2/data_model/simulation.py`；
- `src/tau2/cli.py`；
- `src/tau2/runner/checkpoint.py`；
- `src/tau2/runner/batch.py`；
- benchmark runner；
- metrics 和 judges CLI；
- Gaia2 相关脚本和文档。

增加统一配置常量，例如：

```python
RESULTS_DIR = DATA_DIR / "results"
```

所有新代码通过该常量解析结果目录，不再散落硬编码 `data/simulations`。

### 6.3 旧结果

不自动搬运现有的 `data/simulations/`，也不重新生成结果。

约定：

```text
data/simulations/   # 历史结果，只读
data/results/       # 新结果
```

旧结果仍可通过显式路径读取，新运行不再写入旧目录。

`.gitignore` 增加：

```text
data/results/*
```

## 七、实施顺序

### 阶段 0：静态依赖盘点

- 搜索旧路径、旧 import 和 CLI 入口；
- 建立旧文件到新文件的映射；
- 列出需要调整的 runner、脚本和文档；
- 不运行全量任务。

### 阶段 1：切换结果根目录

- 创建 `data/results/`；
- 增加统一 `RESULTS_DIR`；
- 修改默认保存路径；
- 修改 view、metrics、judges 的默认读取路径；
- 保留旧目录的显式读取能力。

### 阶段 2：建立 Gaia2 独立语言配置

- 创建 Gaia2 pack 和 metadata；
- 修改 Gaia2 loader；
- 修改 `recorded.py`；
- 让 Gaia2 不再读取 τ English pack；
- 保持 reward 和 metrics 行为不变。

### 阶段 3：建立 Gaia2 manifest

- 实现 `gaia2.json`；
- 实现 Gaia2 manifest 数据模型和 loader；
- 直接按任务组选择任务；
- 接入 `tau2 run --benchmark gaia2`；
- 去除 runner 对 `task_subsets` 的依赖。

### 阶段 4：删除旧 subset 和 benchmark

- 删除 `data/tau2/task_subsets/`；
- 删除旧 τ benchmark JSON；
- 删除 subset loader、CLI 和相关文档；
- 清理旧 benchmark 模型中的 subset 强制字段。

### 阶段 5：删除 τ multilingual data

- 删除 τ 的语言 pack 和扩展；
- 保留 Gaia2 独立 pack；
- 移除 τ 专用 runtime、factory 和 localization；
- 保留通用语言上下文和语音接口。

### 阶段 6：整理历史结果

- 新文档统一使用 `data/results`；
- 给 `data/simulations` 加历史目录说明；
- 将旧结果区分为正式结果、审计证据和临时试跑；
- 最后再决定是否删除旧结果。

## 八、轻量验证边界

不进行以下工作：

- 不重新执行 1000 道 Gaia2 参考任务；
- 不做完整语音矩阵测试；
- 不运行完整 metrics；
- 不重新跑历史 τ benchmark；
- 不把数据整理检查写入 reward 或评测得分。

只做机制级确认：

1. manifest 可以读取；
2. manifest 任务 ID 都存在；
3. Gaia2 loader 不读取 τ multilingual 数据；
4. `--dry-run --num-tasks 1` 可以建立 Gaia2 运行配置；
5. text 和 voice 各做一次最小本地启动检查；
6. 新运行输出确实落到 `data/results`；
7. 新运行代码不再默认写入 `data/simulations`。

这些检查只验证数据和路径机制，不改变评测结果，也不成为正式评分流程的一部分。

## 九、主要风险和处理方式

### Gaia2 仍依赖 τ English pack

处理方式：先完成 Gaia2 pack 迁移，再删除 `data/tau2/multilingual/`。

### benchmark 模型仍强制要求 subset

处理方式：先实现 Gaia2 manifest loader，再移除 subset 字段和旧 loader。

### 历史脚本仍写入 `data/simulations`

处理方式：区分新运行脚本和历史审计脚本。新脚本统一使用 `RESULTS_DIR`，历史脚本允许显式指定旧目录。

### 删除旧数据后无法追溯历史结果

处理方式：第一阶段只删除明确不再使用的活动 τ 数据；`archives` 和 `simulations` 暂时保留。

### Modify/Cancel/Hidden Intent 被误当作基础任务

处理方式：manifest 为每个任务组保存独立状态和 scoring mode，任务加载和结果汇总都保留组信息。

## 十、推荐执行顺序

```text
新结果路径
→ Gaia2 独立语言配置
→ Gaia2 manifest
→ runner 脱离 task_subsets
→ 删除 task_subsets
→ 删除旧 benchmarks
→ 删除 τ multilingual data
→ 清理 τ 专用 src
→ 整理旧 simulations
```

每一步都先完成代码路径切换，再删除旧数据，避免活动运行依赖被提前切断。


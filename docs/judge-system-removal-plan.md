# 独立 Judge 体系清理计划

状态：已实施。下文保留实施范围与步骤；末尾记录验证结果。

## 目标

删除独立的事后 `tau2 judges` 体系，以及已经弃用的 Naturalness、Speech Fidelity、Tool Use 和语言专项 judge。

清理后，普通评测继续使用任务结果、ENV_ASSERTION、现有 diagnostics、语音交互指标、状态交互指标和 persistent task 评分。

评测运行中仍然需要的语义检查、用户模拟检查、执行诊断和后台回复判断继续保留。它们属于现有运行或诊断链路，不属于独立的 `tau2 judges` suite。

本计划依靠删除入口、类型、实现和下游依赖完成清理。不向评测系统增加“检查是否调用 judge”的运行时验收步骤，也不重新运行完整题集或调用真实模型。

## 清理后的目标结构

```text
tau2 run
  ├── 任务结果与 ENV_ASSERTION
  ├── intent / execution / interaction / conciseness
  ├── voice interaction
  ├── state interaction / grounding
  └── persistent task 结果

tau2 judges
  └── 删除

Naturalness / Speech Fidelity / Tool Use
  └── 删除
```

## 保留边界

以下模块虽然可能使用 LLM 或包含 `judge` 命名，但仍属于当前运行评分、用户模拟或诊断链路，应继续保留：

- `src/tau2/evaluator/evaluator_communicate.py`
- `src/tau2/evaluator/evaluator_nl_assertions.py`
- `src/tau2/evaluator/review_llm_judge.py`
- `src/tau2/evaluator/review_llm_judge_user_only.py`
- `src/tau2/diagnostics/execution/native/soft_judge.py`
- `src/tau2/diagnostics/intent/`
- `src/tau2/diagnostics/execution/`
- `src/tau2/diagnostics/interaction.py`
- `src/tau2/diagnostics/conciseness/`
- `src/tau2/diagnostics/state/`
- persistent task 的结果和回复判断

这些模块的调用入口、评分含义和数据流不在本次独立 Judge 清理范围内。

## 直接删除的内容

删除以下目录和文件：

```text
data/tau2/judge_catalog/
src/tau2/judges/
src/tau2/data_model/judges.py
src/tau2/multilingual/factory/judge_authoring.py
tests/test_judges/
```

删除内容包括：

- 五个 Naturalness 候选 JSON；
- `production_judges.json`；
- Naturalness、Speech Fidelity、Tool Use 实现；
- judge suite、summary、report、submission、validation；
- judge authoring、candidate 和 promotion 流程；
- judge 专用数据模型和测试；
- `tau2 factory` 的 judge authoring 支持。

`data/simulations/` 和 `data/archives/` 暂时保留。它们包含历史运行结果、审核记录和发布材料，不属于新运行所需的源代码依赖，后续单独按数据保留策略清理。

## 主 CLI 清理

修改 `src/tau2/cli.py`：

- 删除 `from tau2.judges.cli import add_judges_args`；
- 删除 `judges` 子命令注册；
- 移除 `tau2 judges suite`、`summarize`、`export`、`candidate`、`validate`、`promote`。

修改 `src/tau2/multilingual/factory/cli.py`：

- 删除 `add_judge_authoring_args` 导入和调用；
- 移除 `draft-judge`；
- 移除 `compile-judge`；
- 移除 `register-judge`；
- 移除 `list-judges`。

语言包 factory 的其他命令保持不变。

## Leaderboard 和发布链路

独立 Judge 当前仍被 Voice V2 发布流程引用。删除 Judge 包前，需要先断开这些依赖。

### `prepare_submission.py`

修改 `src/tau2/scripts/leaderboard/prepare_submission.py`：

- 删除 `JUDGED_BENCHMARKS` 导入；
- 删除 `production_catalog` 导入；
- 删除 `collect_diagnostics`；
- 删除 `_judge_gaps()`；
- 删除不完整提示中运行 `tau2 judges suite` 的建议；
- 删除 Voice V2 必须拥有每个 Judge verdict 的条件；
- 删除 `judge_coverage` 和 `diagnostics` 的构造与传递。

提交包完整性只由 reward matrix、frame、metadata 和基础运行证据决定。

### `benchmark_package.py`

修改 `src/tau2/scripts/leaderboard/benchmark_package.py`：

- 删除 `BenchmarkDiagnostics` 导入；
- 删除 `DIAGNOSTICS_FILE_NAME`；
- 删除 `diagnostics` 字段；
- 删除 `benchmark-diagnostics.json` 的写入和读取；
- 删除 `BenchmarkPackage.check()` 中关于 Judge diagnostics 的检查；
- 删除 `JUDGED_BENCHMARKS` 判断。

建议暂时保留 `benchmark-quality.json`，但将其定义为运行 provenance sidecar，不再保存独立能力分数、Judge contract 或 verdict fingerprint。

### `benchmark_quality.py`

修改 `src/tau2/scripts/leaderboard/benchmark_quality.py`：

- 删除 `JudgeCatalog` 导入；
- 删除 `collect_quality()` 的 `catalog` 参数；
- 删除调用方构造 production catalog 的逻辑；
- 保留 simulation ID、task ID 和终止原因等运行 provenance。

### `voice_v2_publication.py`

修改 `src/tau2/scripts/leaderboard/voice_v2_publication.py`：

- 删除 `tau2.data_model.judges` 导入；
- 删除 `tau2.judges.submission` 导入；
- 删除 `--judges-root`；
- 删除 `_saved_verdicts()`；
- 删除 `_judge_diagnostics()`；
- 删除 Judge catalog revision 校验；
- 删除 verdict 路径、重复 verdict 和缺失 verdict 检查；
- 将历史 Voice V2 生成流程改为验证 reward、frame、metadata 和 trajectory。

如果后续不再读取旧 Voice V2 发布包，可以继续删除历史 Judge 发布适配逻辑。第一阶段保留奖励和轨迹验证，减少发布工具的变化范围。

### 其他 leaderboard 文件

同步修改：

- `src/tau2/scripts/leaderboard/cli.py`
- `src/tau2/scripts/leaderboard/review_submission.py`
- `src/tau2/scripts/leaderboard/MAINTAINER.md`

删除 `--judges-root`、Judge verdict 上传、Judge suite 和 Voice V2 Judge diagnostics 的说明。

## 文档清理

删除或重写以下文档中的独立 Judge 内容：

- `docs/judge-validation.md`
- `docs/cli-reference.md` 中的 `tau2 judges` 部分；
- `docs/cli-reference.md` 中的 Judge authoring 命令；
- `docs/leaderboard-submission.md` 中的 Judge suite 步骤；
- `docs/voice-v2-publication.md` 中的 Naturalness、Speech Fidelity 和 Tool Use 评分说明；
- `docs/glossary.md` 中的独立 Judge 定义。

更新以下文档的当前口径：

- `docs/metrics-inventory.md`
- `docs/metrics-cleanup-plan.md`
- `docs/metrics-cleanup-followup-plan.md`
- `docs/running-the-benchmark.md`

新口径应说明：

- Naturalness 已弃用；
- Speech Fidelity 已弃用；
- Tool Use Judge 已弃用；
- `tau2 run` 不执行事后独立 Judge；
- 当前指标来自任务结果和现有 diagnostics；
- 新运行不生成 `verdicts.json`。

## 测试调整

删除：

```text
tests/test_judges/
```

删除或改写下列文件中只服务于独立 Judge 的部分：

- `tests/test_benchmark/test_judged_submission.py`
- `tests/test_benchmark/test_matrix_workflows.py`
- `tests/test_metrics/test_cleanup.py`
- `tests/test_benchmark/test_preset.py`

保留以下测试范围：

- ENV_ASSERTION；
- intent、execution、interaction、conciseness；
- persistent task；
- user grounding 和 hallucination retry；
- voice interaction ticks；
- leaderboard reward/package 基础校验。

不新增“运行时禁止 Judge”的测试或验收步骤。删除入口和依赖后，现有导入、CLI 解析和基础评分测试即可发现残留引用。

## 数据处理

### 当前阶段删除

删除：

- `data/tau2/judge_catalog/`；
- 生成 `verdicts.json`、`candidate-results.json` 和 Judge authoring candidate 的代码入口。

本次不批量查找或删除历史输出文件。

### 当前阶段保留

暂时保留：

- `data/simulations/` 下的历史 diagnostics；
- `data/archives/` 下的 Judge review 和验证材料；
- 历史 leaderboard package 中的 `benchmark-diagnostics.json`；
- 旧文档和历史报告中的 verdict 示例。

这些内容属于历史材料，等代码清理完成后再按“正式发布材料、审计证据、临时试跑”分类处理。

## 实现顺序

建议分为四个小提交，降低每一步的耦合范围。

### 提交一：先断开发布链路

- 去掉 leaderboard 对 `BenchmarkDiagnostics` 的依赖；
- Voice V2 不再要求 Judge verdict；
- 保留现有 Judge 包，确保发布工具能够先切换到 reward-only 结构；
- 更新相应 package 测试。

### 提交二：移除 CLI 和 authoring

- 删除 `tau2 judges` 主 CLI；
- 删除 factory Judge authoring 命令；
- 删除 `judge_authoring.py`；
- 更新 CLI 文档。

### 提交三：删除独立 Judge 包

- 删除 `src/tau2/judges/`；
- 删除 `src/tau2/data_model/judges.py`；
- 删除 `data/tau2/judge_catalog/`；
- 删除 Judge 专用测试；
- 清理所有残留导入。

### 提交四：同步文档和历史引用

- 清理 leaderboard、metrics 和 voice-v2 文档；
- 清理旧命令示例；
- 更新 package schema 说明；
- 删除独立 Judge 术语和失效路径。

## 轻量开发验证

不运行完整 benchmark，不重新执行任务，不全量回放，不调用真实模型，不向评测运行增加验收步骤。

只做以下开发时检查：

```bash
UV_FROZEN=1 uv run tau2 --help
UV_FROZEN=1 uv run tau2 factory --help   # 若语言 factory 仍安装
UV_FROZEN=1 uv run pytest tests/test_benchmark/test_preset.py -k complete_saved_matrix
uv run ruff check <改动文件>
uv run ruff format --check <改动文件>
```

检查重点：

- `tau2 judges` 不再出现在主 CLI；
- factory 不再暴露 Judge authoring 命令；
- `tau2 run` 的任务评分入口可以导入；
- Voice V2 package 不再要求 `benchmark-diagnostics.json`；
- 其他 diagnostics 不再导入 `tau2.data_model.judges`；
- 新运行不会生成 `verdicts.json`；
- 没有新增模型调用；
- 任务数据、快照、资源和评分事实没有被改写。

## 结果和风险

清理完成后，独立的 `tau2 judges`、Naturalness、Speech Fidelity、Tool Use 和语言专项 Judge 将从入口、类型、实现、发布流程和候选数据中移除。

其他评分模块仍可能使用“judge”命名或 LLM 判定，但它们继续服务于当前任务评分、用户模拟和诊断流程，不能作为独立 Judge 数据清理。

历史 Judge verdict 和发布包默认保留，因此不会因为删除代码而丢失已有审计材料。若不再需要读取旧 Voice V2 Judge 包，后续可以再单独清理这些历史文件。


## 实施记录

已移除独立 Judge 包、模型、候选数据、CLI、factory authoring 和专用测试；
CI 不再收集已删除的 `tests/test_judges`。发布工具不再读取、校验或上传 verdict，
新包不导出 `benchmark-diagnostics.json`。`benchmark-quality.json` 的现有结构保留，
仅解除 Judge 类型和 catalog 参数依赖。

同步了使用文档、CLI 参考、语言贡献模板与本地贡献技能中的失效命令。
历史轨迹、归档和已发布包没有重写或批量删除。当前任务 evaluator、用户模拟检查、
语音指标和 diagnostics 实现没有因本次清理而修改。

开发时验证：

- 当前评分、转写及合成 matrix 相关用例曾通过 109 项；
- 历史发布测试缩为 8 条合成调用，10 个测试全部通过，不依赖旧发布题库；
- 普通 matrix 的完整打包与重读用例在最后一次检查中通过 3 项；
- 主 CLI 可加载且不再包含独立 Judge 命令；
- 改动 Python 文件的 Ruff、格式与差异空白检查通过；
- 源码、测试和 CI 中不再引用已删除的 Judge 包、数据模型或 authoring 命令。

同一工作区另有数据整理改动，已移除旧 task-subsets 模块和发布语言包。
依赖这些材料的旧发布/语言测试以及独立 factory parser 检查因此不能完整运行；
本次没有恢复它们或调整那些工作流。未运行完整评测、全量回放或真实模型，
没有给评测系统增加验收步骤，也没有创建提交。

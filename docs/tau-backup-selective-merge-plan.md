# `tau-backup` 选择性合并计划

本文规划如何把 `origin/tau-backup` 中有价值的修改移植到当前 async persistent-task 工作区。

当前比较基于：

- 共同基线：`3fb4011`
- `tau-backup`：`4961bee`，提交信息为 `chore: preserve tau backup workspace`
- 当前工作区：在 `3fb4011` 上继续开发，包含尚未提交的 Gaia2 async、persistent task 和 Qwen observer 改动

`tau-backup` 是一条独立的备份和稳定性修复线。它包含旧的 1000-task Gaia2 数据、备份 checkout 路径和历史验证材料。当前工作区已经扩展到 1100 个任务，因此不直接 cherry-pick `4961bee`，也不覆盖当前数据文件。

## 合并目标

最终保留当前工作的核心能力：

- 100 个 Gaia2 async 任务及其数据 seal；
- provider-neutral persistent-task runtime；
- Qwen 后台任务 observer 和 evidence queue；
- generated-text、played-audio 和 reply-window 评分；
- 当前 full-duplex stage、turn boundary 和媒体时钟逻辑。

从 `tau-backup` 中选择性加入：

- Qwen Docker 启动超时后的容器恢复和清理；
- MiMo TTS 临时连接错误的重试；
- diagnostics provider timeout retry；
- 语音 ASR 场景的 NL judge 规则；
- 经过审计的 Gaia2 literal/location 容错规则；
- 必要的审计文档和测试证据。

## 不直接合并的内容

| 内容 | 原因 |
| --- | --- |
| `data/tau2/domains/gaia2/manifest.json` | backup 是 1000 个任务，当前是 1100 个任务，hash 和 runtime 版本不同 |
| `data/tau2/domains/gaia2/tasks.json` | backup 不包含当前 100 个 async 变体 |
| `data/tau2/domains/gaia2/verification.json` | backup 的任务和 runtime seal 对应旧数据 |
| `data/tau2/domains/gaia2/split_tasks.json` | backup 没有 `async` split |
| backup 的绝对 assets 路径 | 指向 `tau2-bench-backup/data/...`，会破坏当前 checkout |
| `full_duplex_orchestrator.py` 整文件 | 当前文件已经在原有 transcript 修复上加入 persistent-task 调度逻辑 |
| `evaluator_nl_assertions.py` 整文件 | 当前文件已经加入 persistent generated-text 对齐豁免 |
| `diagnostics/model.py` 整文件 | backup 使用 `tau-json/1`，当前使用 `tau-json/2` |
| `scripts/run_backup_smoke.sh` 原文件 | 脚本假设根目录是 backup checkout |

## 阶段 0：建立当前工作区 checkpoint

在开始移植前，先保存当前未提交工作。建议在本地创建 WIP checkpoint，不推送到远端。

checkpoint 必须包含：

- 当前 tracked 修改；
- `src/tau2/persistent_tasks/`；
- `src/tau2/evaluator/persistent_tasks/`；
- Gaia2 async 数据和 metadata；
- Qwen observer、配置和验收脚本；
- persistent-task 测试和文档。

之后在独立的 integration 分支上工作。若不使用 WIP commit，至少需要同时保存 tracked diff 和所有 untracked 文件清单，避免只保存 `git diff` 而漏掉新文件。

## 阶段 1：移植 Qwen Docker 容器恢复

涉及文件：

```text
src/tau2/voice/audio_native/qwen_agent/runtime.py
tests/test_voice/test_audio_native/test_qwen_agent.py
```

`tau-backup` 为每次 Docker 启动生成唯一 label，并把 label 加入 `docker run`。如果 CLI 在写入 cidfile 前超时，代码会按这个 label 找回当前启动创建的容器，并清理重复容器。

预期行为：

1. cidfile 已写入时，仍按 cidfile 清理；
2. cidfile 未写入但容器已经创建时，按 label 找回；
3. 同一 label 找到多个容器时只保留一个；
4. 找不到容器时保留原始异常；
5. 不影响其他评测实例的容器。

当前工作区没有修改 `runtime.py`，因此这部分优先级高、冲突较小。测试文件已经包含当前 async backend queue 测试，只加入 Docker recovery 的单个测试，不覆盖整个测试文件。

## 阶段 2：移植 MiMo TTS 连接重试

涉及文件：

```text
src/tau2/voice/mimo.py
tests/test_voice/test_mimo.py
```

`tau-backup` 新增 `MiMoTTSConnectionError(ConnectionError)`，把底层 `httpx.HTTPError` 转换成可被现有 `tts_retry` 识别的连接异常。

合并要求：

- 保留当前 MiMo 配置解析和响应校验；
- 临时连接错误可以重试；
- 认证错误和响应格式错误不能被伪装成连接错误；
- 错误文本不包含 URL、密钥或响应 body。

## 阶段 3：手工合并 diagnostics timeout retry

涉及文件：

```text
src/tau2/diagnostics/model.py
tests/test_diagnostics/test_model.py
tests/test_diagnostics/test_conciseness.py
```

当前 `TauJsonModel.complete_sync()` 同时负责 cache identity、budget、provider 调用、attempt 记录和 JSON 解析。合并时应把当前主体抽成 `_complete_once()`，然后在外层加入 retry。

目标结构：

```text
_complete_once()
    保留当前 tau-json/2 缓存和 attempt 逻辑

complete_sync()
    timeout 最多重试一次

complete()
    对单次线程调用增加外层 deadline
```

必须保留：

- 当前的 `implementation: tau-json/2`；
- 当前 cache identity 和成功结果格式；
- 当前 `RequestBudget`；
- provider error 和 malformed response 的 attempt 记录。

建议行为：普通 `ValueError`、schema 错误和认证错误不重试；timeout 最多重试一次；每次真实 provider 调用都消耗一次 budget；第一次 timeout 的 attempt 仍然保留；已有成功 cache 不重复调用 provider。

需要注意，`asyncio.wait_for(asyncio.to_thread(...))` 无法强制终止已经卡住的线程。外层 timeout 解决的是评测主流程被永久阻塞的问题，但可能让旧线程和 retry 短暂并行。

验收测试：

1. 普通错误只调用一次；
2. timeout 后第二次调用成功；
3. 两次 timeout 后返回错误；
4. budget 不允许 retry 绕过请求上限；
5. async outer deadline 不会无限等待。

## 阶段 4：手工合并语音 NL judge 规则

涉及文件：

```text
src/tau2/evaluator/evaluator_nl_assertions.py
```

`tau-backup` 增加的 judge prompt 允许语音转写造成的大小写、标点、引号、空格和分隔符差异，同时继续严格要求单词、业务对象、联系人、ID、email、数字、日期、时间和 canonical 环境值。

当前工作区已经增加 `allow_unresolved_live_audio`，用于 persistent generated-text 评分。因此不能用 backup 文件覆盖当前文件，需要把 prompt addendum 和现有 audio alignment 参数叠加。

backup 的 addendum 当前对普通 NL evaluator 也会生效。合并时应改成只对 full-duplex/voice evaluator 使用，避免文本任务意外获得 ASR 容错。

验收：

- `Properties List` 和 `properties list!` 可以判为一致；
- `Property List` 和 `List Properties` 仍失败；
- 错误收件人、ID、金额和日期仍失败；
- 普通 text NL judge 不使用 voice addendum；
- persistent generated-text 路径继续保留当前 alignment 豁免范围。

## 阶段 5：评估 Gaia2 literal/location 规则

涉及文件：

```text
src/tau2/domains/gaia2/location_rules.py
src/tau2/domains/gaia2/scoring.py
tests/test_domains/test_gaia2/test_gaia2.py
```

这部分会改变 Gaia2 业务判分，应在前面稳定性修改完成后再做。

`tau-backup` 的 `_same_words()` 忽略大小写、标点和连接符，但保留单词顺序和重复次数。只有经过审计的 source task allowlist 才启用这种 literal 容错，不能全局放宽字符串匹配。

`VOICE_LOCATION_RULES` 对地点字段提供两种模式：`user` 按用户说出的地点词比较；`mixed` 除了地点词一致，还要求结果中包含环境定义的关键片段，例如街道、邮编或城市。

移植前需要确认 async task 能从派生 ID 找回原始 source ID。若不能，需要在 scoring 中显式读取 `scenario.source_id` 或 `async_metadata.source_task_id`。

必须继续严格的字段：联系人和任务 ID、email、电话、收件人、日期、时间、数字、金额、环境 canonical 值，以及 persistent task 状态和 result。

测试要求：

1. 非 allowlist literal 仍然严格；
2. allowlist 只放宽大小写和标点；
3. 单词顺序改变仍失败；
4. `mixed` 缺少环境片段时失败；
5. async 派生任务能正确继承原始 source rule；
6. task 总数仍为 1100，原有 category split 不变。

修改 scoring 后必须重新执行 Gaia2 verification/seal 流程，不能手工修改 manifest 的 `runtime_sha256` 或 `tasks_sha256`。

## 阶段 6：整理 audit 文档和复现脚本

如果最终采用 location rules，建议保留以下审计材料，并注明对应的 scoring version、allowlist 版本和生成依据：

```text
docs/voice_exactness_risk_audit.md
docs/voice_location_source_audit_48.md
docs/voice_source_audit_0070.md
docs/voice_source_audit_all.md
```

以下 DeepSeek 脚本主要复现历史诊断问题，按需保留：

```text
scripts/replay_deepseek_stuck_prompts.py
scripts/reproduce_diagnostics_litellm.py
scripts/reproduce_long_eval_then_repetition.py
```

如果要纳入当前项目，应把硬编码的历史路径改成显式参数，例如 `--run-root`、`--config`、`--output` 和 `--model`。它们不属于 persistent-task 主运行链路。

`scripts/run_backup_smoke.sh` 是 backup checkout 专用脚本。可以参考它的环境隔离逻辑，但应为当前项目重新写 wrapper，不能直接复制。

## 提交顺序

建议拆成以下小提交：

```text
1. fix: recover Qwen containers after Docker launch timeout
2. fix: retry transient MiMo TTS connection failures
3. fix: retry diagnostic model timeouts without changing cache identity
4. fix: preserve voice ASR semantics in NL judge
5. feat: add audited Gaia2 voice literal and location matching
6. docs: preserve voice scoring audit provenance
7. chore: add adapted diagnostic replay tools, if needed
```

每个提交都应保持当前 async 数据和 persistent-task 结构不变。

## 验收顺序

先做无 API 的定向检查：

1. Qwen Docker recovery 单测；
2. MiMo retry 单测；
3. diagnostics retry 单测；
4. NL judge 规则测试；
5. Gaia2 scoring/location 测试；
6. persistent-task core 和 streaming 测试；
7. `tau2 check-data`；
8. targeted Ruff 检查。

所有离线检查完成后，再进行有限的 live smoke。live 验收需要分别记录 provider 支持、backend evidence 完整性、task 触发、native operation 归因、reply window 归属，以及 Gaia2 原始 reward 和 persistent reward 是否分开。

## 完成标准

合并完成后，最终工作区应满足：

- 当前 1100-task async 数据仍可验证；
- 普通 Gaia2、voice 和 persistent-task 流程都能导入；
- Qwen Docker timeout 不会留下孤儿容器；
- transient MiMo 错误可以按现有 retry 策略恢复；
- diagnostics timeout retry 不破坏 `tau-json/2` 缓存；
- voice NL judge 只放宽 ASR 表达差异，不放宽业务事实；
- Gaia2 location 容错有 allowlist、审计来源和测试；
- backup 历史数据不会覆盖当前 async manifest 和 runtime seal。

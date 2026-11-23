# 场景时间注入实施记录

## 实现

Qwen Audio Agent 源码位于相邻的 `qwen-audio-agent` 仓库。本次从与原测试镜像相同的 `d2a8dc45b9b49d560c876eb7ed449eb9b90df1b9` 实施，新增本地提交 `02426d5`（`feat: allow embedders to provide frontend current time`）。没有推送源码或镜像。

Gateway 增加可选的 `getCurrentTimeSnapshot` 回调，沿原构造链传至前台 `get_current_time`。未提供回调时仍使用原格式化函数和真实时间；回调失败返回普通工具错误，不回退到主机日期。

tau bridge 检查公开工具列表，包含 `System__get_current_time` 时，通过已有 ToolBroker 转发前台查询，取同一个场景时钟的 epoch 秒并复用原格式化函数。兼容 tau 工具序列化后的数值字符串。每次查询重新读取，不缓存、不轮询、不另建时钟。ready metadata 记录时间来源。

交互评分输入增加 Gaia2 场景起始时间、UTC 和已有时间工具记录；历史纯对话证据可携带题目公开的起始时间，但不会补造工具记录或声称旧 Agent 已采用新的时间来源。交互评分版本更新为 `tau-interaction-effectiveness/3`。

## 针对性离线检查

- Gateway 到前台工具的完整注入链路通过：注入固定日期、默认真实时间、查询失败三个分支；启动时不提前读取。
- bridge 异步转发通过：连续两次不同时间、数值/数值字符串、失败及无效结果；失败不调用主机格式化作为后备。
- 评分输入及同文件内其他直接相关用例：10 个通过。新增用例覆盖公开时间背景、仅保留时间查询记录、无记录的历史会话。
- 修改的 Python 文件通过 Ruff 检查和格式检查；bridge 通过 Node 语法检查。

完整运行 `tests/test_diagnostics/test_interaction.py` 时另有 3 个既有汇总/CLI 用例失败。将 interaction 模块替换为本次修改前的 HEAD 实现、保留当前工作区其他内容后，三个失败仍能复现：汇总缺少 `interaction` 键、CLI dry-run 不再输出旧的 `target_utterances`、历史运行汇总缺少 `records` 键。本次没有调整这些既有汇总/CLI 行为。

没有新跑完整评测题，也没有调用评分模型。时间接口没有添加逐 tick 对时、漂移阈值、额外模型调用或因时间证据缺失取消整场评分的条件。

## 覆盖边界

本次统一指定时间查询工具的来源。原生提醒调度、任意 shell/代码中的系统时间、场景时钟暂停仍遵循原实现；不能将本次变更理解为这些能力已经完成适配。重复追问评分规则也没有在这次修改。

## 本地镜像与配置

- 镜像：`docker.v2.aispeech.com/sjtu/sjtu_yukai-tau-qwen-agent:0.5.4-scenario-clock`。
- Image ID：`sha256:c83be2a15280369338fd2d136cdf1e62d9a8105e633329ed6cf7c53698335cc4`。
- 源码完整 revision：`02426d53050d3e7be59d45e5b4646e3c4e7c0dd9`。
- 新源码 git archive SHA256：`0db6f930ec9db5f5bb5668faf45243798e83d8188ac748947c6c31a905c55aec`。
- 复用依赖的基底 image ID：`sha256:923167978bbdddbe734f2bb0ded38bdf756a399d0c0447a4ca31aab414659006`。
- 已更新 `integrations/qwen_audio_agent/config.gaia2.dynamic.metrics.deepseek-user.json` 的镜像版本，其他运行配置没有切换。

最初运行正常的完整构建，但新的 Node 基础镜像下载持续缓慢，因此终止这次完整构建，使用上述本地基底制作源码增量层。构建前确认原基底记录的源码归档 SHA256 正确；新提交相对于该源码仅修改本次 6 个 `.mjs` 文件，没有修改依赖或删除源文件；两个 integration 依赖锁与基底相同。新层包含这些源码文件、当前 bridge 和新的真实 provenance，不沿用旧源码标识。

构建配方保存在当前工作区的 `build/scenario-clock/local-image/Dockerfile`，构建日志为 `build/scenario-clock/local-image-build.log`。镜像内再次运行两组对应的离线测试，均通过；构建阶段禁用网络，没有额外依赖安装或模型调用。配方是本次本地构建产物，不是新增常驻构建系统；后续完整重建仍可使用现有 `build_image.py` 和上述干净源码 revision。

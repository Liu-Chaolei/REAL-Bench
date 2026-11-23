# Gaia2 分阶段 observer ASR 热词

当前主流程默认已更新为 [v2](../v2/README.md)，新增 GT 工具返回值中的困难专名并复查旧词。本目录保留为原始抽取记录与回退词表。

这份词表通过 DeepSeek 离线抽取，覆盖当前 `data/tau2/domains/gaia2/tasks.json` 中全部 1100 条任务，包括 100 条 async 变体，每条任务 3 个阶段。它供后续按阶段启用 observer ASR 热词使用，不修改原任务、工具 GT 或评分断言。

## 本次完成结果

- 1100/1100 条任务、3300/3300 个阶段全部完成并通过来源、结构与累计启用规则校验，包含 100 条 async 变体。
- 全局按忽略大小写去重后共 3281 个词；每条任务最终累计平均 7.769 个、中位数 7 个，最多 49 个。8 条任务的三个阶段均没有选出热词，197 份阶段累计词表为空；空词表是允许结果，不代表抽取失败。
- 386 条任务发生过重试，共保留 1609 次 API 尝试记录。最后 1 条因多阶段 JSON 重复字段反复失败，改为每阶段单独发送相同 prompt，再经过相同的来源与累积词表校验后完成；没有手工补写词语。
- 模型为 `deepseek-flash`。原始 API usage 合计：5,360,265 prompt tokens、775,071 completion tokens，共 6,135,336 tokens，包含纠错重试。
- 完成后再次离线遍历全部文件，复核每个词的同阶段原文来源、逐阶段累积词表、50 的权重、50 词上限，以及逐次调用记录；结果见 `VALIDATION.json`。

## 文件

- `manifest.json`：任务集合、source 数据与 prompt 的哈希、模型、抽取参数及词表策略。
- `prompt.txt`：本次实际使用的完整筛词 prompt，与 task 0007 离线试验相同。
- `tasks/<完整任务 ID>.json`：每条任务的分阶段抽取结果与来源、累计词表、抽取模型和调用信息。
- `profiles.jsonl`：按原任务顺序汇总的逐任务记录，每行一条；由脚本汇总生成。
- `summary.json`：完成数量、阶段数量、词数分布、重试统计及 API usage。只有 `complete=true` 才表示全部任务均通过本地校验。
- `vocabularies.tsv`：便于表格软件查看的 3300 行阶段词表，含任务 ID、本阶段新增词、累计词及数量。
- `VALIDATION.json`：全量离线复核结论、词数统计和汇总文件哈希。

逐次请求、DS 原始回答和错误记录位于 backup 内的 `data/simulations/gaia2-stage-hotwords-1100-20261122/`。该目录被 Git 忽略，通常不会随提交传输。

## 抽取与启用规则

只输入每个阶段的 source request，以及该阶段 reference 工具调用参数中的字符串和 reference agent 回复文本。不输入环境快照、真实模型对话、评分断言或未引用的环境字段。GT 与 source 的词语同样可以入选。

LLM 挑选难识别的人名、地名、组织、品牌、技术名词与特殊自定义名称；排除普通活动词、日期时间、邮箱、内部 ID、工具名称和完整句子。每个词记录 `text`、`kind`、`source` 和中文 `reason`。

代码检查结构化输出、重复 JSON 键、阶段完整性与顺序，并要求每个词在其所引用的**同一阶段**原文中连续出现。比较前仅规范 Unicode NFC 和空白，不放宽拼写或大小写。包含邮箱、无字母、超长或 UUID 等不合格候选会被拒绝。模型出错时通过追加纠错请求重试，不用人工编造词语或将非法候选直接当作成功。

阶段内词语按忽略大小写去重。`stage0` 仅启用阶段 0 的词；`stage1` 启用阶段 0 与 1 的并集；`stage2` 启用阶段 0、1、2 的并集。不提前启用未来阶段的词。同一词后续再次出现时保留首次出现的拼写。

每个阶段记录：

| 字段 | 含义 |
| --- | --- |
| `extracted` | 本阶段 LLM 选择的词语及原文来源 |
| `new_terms` | 相对已释放阶段新增的词 |
| `active_vocabulary` | 到该阶段为止累计启用的 `{词语: 权重}` |
| `vocabulary_sha256` | 对实际累计词表计算的哈希 |

权重由代码统一设为 **50**，每阶段累计词表最多 50 个词，不要求填满。单词的“是否足够难”属于 LLM 的选择判断，本地来源与格式校验不等同于对语义筛选进行人工逐项审核。

## 调用与复现

使用配置中的 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 引用。本次实际模型记在 `manifest.json`，初始化为 `deepseek-flash`。参数为 `temperature=0`、`seed=42`、关闭 thinking，要求 JSON 输出。每条任务独立调用，一次包含三个阶段；错误时最多尝试四次，格式/来源错误会将校验信息反馈给模型。输出截断时可提高到 6000 个输出 tokens。

```bash
# 正常抽取；已完成且哈希一致的任务会直接复用
PYTHONNOUSERSITE=1 UV_FROZEN=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -B scripts/extract_gaia2_stage_hotwords.py --workers 8

# 只检查所有保存词表并重新生成汇总，不调用 LLM
PYTHONNOUSERSITE=1 UV_FROZEN=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -B scripts/extract_gaia2_stage_hotwords.py --verify
```

模型调用的原始回答会保存，任务数据或 prompt 哈希变化时拒绝静默复用。不同时间重新调用 LLM 未必逐字得到同一结果，复现应优先使用这份固定产物。

对于普通重试仍无法通过的少量任务，可加 `--split-stages`，保持同一筛词 prompt，改为每个阶段一次请求，再合并校验。已成功任务仍从缓存读取，不重新调用。

## 主流程接入状态

后续已接入主流程。当前 `config.gaia2.realtime.dynamic.metrics.deepseek-user.json` 使用以下配置：

```json
{
  "asr_hotwords": {
    "enabled": true,
    "mode": "gaia2_stage_gt",
    "weight": 50,
    "max_terms": 50
  }
}
```

运行时从本目录读取当前 task ID 的词表，先校验 source/GT 指纹与同阶段来源；不再调用 DS 抽取，不扫描环境。默认目录位于 `TAU2_DATA_DIR/gaia2_stage_hotwords/v1`，也可通过 `asr_hotwords.catalog_dir` 指定。

初始启用 stage0；当 stage1/2 请求开始实际语音传输时，再启用此前与当前阶段的词。只排队或生成新请求，不提前切换词表。切换时结束旧的 ASR 片段；每个新会话固定保存自己的词表，已排队的旧音频不会被套用新阶段词。热词仅发送给 user 端 observer ASR，agent 前后端不会收到词表。

此模式要求 English Gaia2 和 Qwen 流式 observer；缺失或过期的词表会在 agent 启动前报错。其他配置仍可选择旧的 `user_background` 模式或关闭热词。生成时的 `VALIDATION.json.runtime_hotwords_enabled=false` 记录的是批量抽取完成时的状态，不代表后续主流程配置。

# Gaia2 分阶段 observer ASR 热词 v2

这一版用 `deepseek-flash` 复查全部 1100 条任务的旧热词，并从已保存的 GT 工具运行结果中增量抽取困难专名。每任务的三个阶段一起处理；长文本或持续校验失败时，改为分阶段、小分块调用相同 prompt。完成数量和 token 用量见 [summary.json](summary.json)，运行时全量复核见 [VALIDATION.json](VALIDATION.json)。

## 选择规则

- 输入只有旧词的短列表和对应阶段的 `tool_response.content`，不重发 source task、工具参数和参考回复全文。旧词仍保留回到原任务文本/GT 的来源链。
- 尽量保留困难完整人名、地名、机构名、品牌或特殊名称；带拉丁变音符号的专名优先保留，没有变音符号但难以拼读的完整专名同样可以保留。不要求凑满数量。
- 删除明确普通的短名（例如 Max）、通用词和简单机构名。few-shot 用于解释选择标准。
- 每次请求每阶段最多选择 50 个，避免输出失控；最终词表按阶段累计。Qwen 上限是累计最多 2000 词，其中权重 50 的超级热词最多 50 个。按阶段实际启用的累计顺序，前 50 个词统一使用 50，超过 50 后新增的词使用 5，与来自旧词表还是工具返回值无关；已启用词的权重保持稳定。49 不是有效接口权重。
- 严格校验同阶段连续原文引用。若 LLM 只标错了 source，而原词确实存在于同阶段另一个来源中，代码修正 source 标签并记录；缺少同阶段原文、邮箱、内部标识和格式错误需要重试。分开的 first_name/last_name 不能冒充连续原文。
- 程序按 Unicode NFC、大小写和词边界做覆盖比较。同阶段先处理长词，被实际保留的完整短语覆盖的短词不再新增。未保留的旧词、未来阶段的词不能用于抑制当前候选。
- 图片/PDF 等 base64 附件不发送给 LLM，其周围文本保留，并记录省略字段的长度与哈希。长文本分块带重叠，不静默截断。

## 文件与来源

- `tasks/<task ID>.json`：分阶段选择、每阶段新增词、累计带权词表、实际引用的文字证据、原词删除列表与来源修正记录。
- `manifest.json`：数据、模型、prompt、权重策略，以及每条冻结词表的校验哈希。只有 `complete=true` 的目录允许加载。
- `prompt.txt`：实际发送的通用 system prompt；`profiles.jsonl` 和 `vocabularies.tsv` 便于批量查看。
- `sources` 中旧词引用可直接对当前 task 的原文/GT 校验；工具返回来源固定到 reference 事件及其阶段，并附带原始返回值哈希。工具返回的引用文字已包含在词表中，因此评测时不依赖完整 GT dump，也不重新执行 GT 工具。
- 全量 GT dump 仍保存在 `../v1/gt_results/`，不加入 Git。逐次请求、原始回答和纠错记录位于 `data/simulations/gaia2-hotwords-v2-1100-20261122/`，同样被 Git 忽略。

## 生成与运行

```bash
PYTHONNOUSERSITE=1 UV_FROZEN=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  LD_LIBRARY_PATH=.venv/lib/tau-native \
  .venv/bin/python -B scripts/review_gaia2_stage_hotwords.py --workers 8

# 只处理未成功任务；复用已成功文件
.venv/bin/python -B scripts/review_gaia2_stage_hotwords.py --workers 8 --split-stages

# 针对未被完整长词覆盖、但被漏选的带变音符号完整人名，再让 DS 复核
.venv/bin/python -B scripts/review_gaia2_stage_hotwords.py --workers 8 --audit-omissions

# 离线汇总校验，不调用 DS
.venv/bin/python -B scripts/review_gaia2_stage_hotwords.py --verify

# 离线修正已保存词条的权重及其哈希，不重跑抽取
.venv/bin/python -B scripts/review_gaia2_stage_hotwords.py --reweight
```

Qwen user-side observer 使用 `mode=gaia2_stage_gt`、`weight=50`、`max_terms=2000`，按各词条的实际 50/5 权重发送。仍在新阶段任务开始实际语音传输时激活对应累计词表，旧音频会话继续使用创建时固定的旧阶段词表。词表不会传给前后端 agent。

旧版 v1 文件保留。需要回退时将 `asr_hotwords.catalog_dir` 指向 `data/gaia2_stage_hotwords/v1`；v1 加载器仍按全 50、累计最多 50 个词校验。

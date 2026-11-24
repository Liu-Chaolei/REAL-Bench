# Gaia2 English runtime

Gaia2 的文本和语音调用使用同一份独立英文配置。活动入口只注册 `gaia2` 环境，题集为 `gaia2` 和 `gaia2_hidden`。旧 τ domain 数据和源码保存在本地 `data/archives/tau-legacy-*`，不再参与运行。

## 数据与代码

- `data/tau2/domains/gaia2/language/en/pack.yaml`：英文规则、caller profile 和语音配置。
- `data/tau2/domains/gaia2/language/en/metadata.json`：任务 ID 与 caller 性别信息。
- `src/tau2/domains/gaia2/language.py`：固定读取 Gaia2 配置，不再发现外部语言包。
- `src/tau2/domains/gaia2/language_runtime.py`：选择题集、解析 caller、构造和记录调用上下文。
- `data/tau2/benchmarks/gaia2.json`：基础题、交互变体、隐藏信息等任务组清单。

语言层只接受英文和 Gaia2。旧的语言扩展、区域数据库、本地化工厂和人工客服转接规则已移除。

## Caller profile

`gaia2_male` 和 `gaia2_female` 是中性任务委托用户。身份和已知事实来自具体题目；profile 不补充地区、职业或新目标，也不要求找主管或反复施压。实际用户系统提示由 `tau2.user.gaia2_guidelines` 构建。

文本模式不携带语音行为。语音模式使用同一 caller 的声线设置、自然确认和显式拼读规则。现有 ElevenLabs 声线作为音色资源继续复用，没有生成新声线或调用付费服务。可用以下环境变量替换对应音色：

```text
TAU2_VOICE_ID_EN_GAIA2_MALE
TAU2_VOICE_ID_EN_GAIA2_FEMALE
```

MiMo 使用 provider 自己的 `gaia2_male: Dean`、`gaia2_female: Chloe` 映射；Qwen 使用其 provider 配置。不要把 ElevenLabs 的 ID 传给其他 TTS provider。

## 运行

文本预览：

```bash
UV_FROZEN=1 uv run tau2 run --benchmark gaia2 --num-tasks 1 --dry-run
```

语音预览：

```bash
UV_FROZEN=1 uv run tau2 run --benchmark gaia2 --audio-native --num-tasks 1 --dry-run
```

去掉 `--dry-run` 才会执行模型调用。Gaia2 manifest 运行默认选择基础 800 题；`--num-tasks` 只缩小本次运行范围。

显式选择改口、重复或交互变体，沿用已有 split：

```bash
UV_FROZEN=1 uv run tau2 run --benchmark none --domain gaia2 \
  --task-split-name repetition --num-tasks 1 --dry-run
```

隐藏信息使用同一个 Gaia2 环境和独立题集：

```bash
UV_FROZEN=1 uv run tau2 run --benchmark none --domain gaia2 \
  --task-set-name gaia2_hidden --task-split-name hidden_intent \
  --num-tasks 1 --dry-run
```

`--language en` 仅作为兼容参数。`tau2 languages`、`tau2 factory`、`tau2 intake-tasks` 和 `--no-localize-db` 已取消。

## 输出与复现

相对 `--save-to NAME` 写入 `data/results/NAME/`，绝对路径按指定位置保存。内部 `simulations/` 分片目录保持原格式。任务、caller、语言 revision 和 artifact digest 随普通运行结果记录，续跑使用已有的内容一致性约束。

新 caller 配置 revision 为 `gaia2-language-v2`。旧结果可以按显式文件路径读取；不同 caller 配置的运行应保存在不同目录。历史归档仍可携带旧式 `pack.yaml + language_extension`，新归档使用 `language/en/`。

任务 JSON、快照和参考答案未因本次语言清理而重写，评分逻辑不承担清理验证。冻结运行时的源代码 pin 仍沿用原有机制；本次未执行参考验证或重新封存全量题库。

## 本次验证范围

只做英文 pack、text/voice context、hidden caller 性别映射、registry 范围和单题 dry-run。没有执行 API 评测、参考轨迹重跑或完整语音矩阵。

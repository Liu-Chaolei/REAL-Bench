> 当前工作区已迁移到 Gaia2-only 评测。任务、英文 caller 配置与运行入口见 [Gaia2 English runtime](docs/gaia2-language-runtime.md)；新结果写入 `data/results/`。下文涉及旧 τ 领域的内容保留历史项目背景。

# REAL-Bench

面向真实世界智能体的端到端评测基准。REAL-Bench 在 τ-bench 的任务、工具和
环境框架之上，加入 Gaia2 场景、Qwen Audio Agent 集成，以及可复现的意图、
执行和语音交互诊断。

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Repository](https://img.shields.io/badge/GitHub-REAL--Bench-181717?logo=github)](https://github.com/Liu-Chaolei/REAL-Bench)

> 项目仍使用 <code>tau2</code> 作为 Python 包名和命令行入口，这是为了兼容已有任务、
> 结果文件和工具链。对外项目名称为 **REAL-Bench**。

## 项目内容

REAL-Bench 评测 Agent 在真实业务环境中的完整行为：理解用户请求、规划并执行
工具调用、处理多轮修改和重复请求、进行语音交互，并在结束后保留足够证据供
复核。评测既可以运行普通文本对话，也可以运行包含 ASR、LLM、TTS、播放、
打断和工具等待的全双工语音通话。

当前仓库的重点包括：

- **Gaia2 任务集**：1,000 道三阶段任务（800 道基础任务、100 道改口变体、
  100 道重复表达变体），另有 AddTask、Modify、Cancel 交互变体。
- **Qwen Audio Agent**：通过独立容器、工作区、MCP 和应用状态运行 Gaia2 Agent。
- **REAL 诊断指标**：意图忠实度、执行质量、语音交互和交互状态指标，与原有
  任务 reward 并列保存。
- **可审计证据**：保存实际交付的用户输入、Agent 播放音频、ASR 片段、后端
  提交、工具参数、应用状态和阶段时间线。
- **可复现运行**：任务、资源、运行时和评分配置带有版本与哈希；Gaia2 运行前
  可通过 reference verification 和 seal 检查数据完整性。

## 评测链路

~~~text
用户任务
   │
   ├── 文本用户模拟器 ──┐
   └── 语音用户模拟器 ──┼── τ orchestrator ── Agent / 工具 / 应用环境
                       │
                       └── trajectory + audio + evidence
                                      │
                                      ├── 原始任务 reward
                                      └── REAL diagnostics
~~~

诊断指标按独立 profile 保存，缺失证据会明确标记为 unavailable，不会静默当作
零分或满分。诊断是评测报告的一部分，不会改写原始任务 reward。

## 快速开始

### 安装

需要 Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)
和（运行语音 Agent 时）Docker。建议使用锁定依赖：

~~~bash
git clone https://github.com/Liu-Chaolei/REAL-Bench.git
cd REAL-Bench

UV_FROZEN=1 uv sync --extra gaia2 --extra voice --extra diagnostics --extra dev
UV_FROZEN=1 uv run tau2 check-data
~~~

只运行核心文本任务时可以省略 <code>gaia2</code>、<code>voice</code> 和
<code>diagnostics</code>：

~~~bash
UV_FROZEN=1 uv sync --extra dev
~~~

主要 extras：

| extra | 用途 |
| --- | --- |
| <code>gaia2</code> | Gaia2 数据导入、资源和验证 |
| <code>voice</code> | 全双工语音、ASR、TTS 和音频处理 |
| <code>diagnostics</code> | REAL 指标依赖 |
| <code>dev</code> | pytest、ruff 和 pre-commit |
| <code>all</code> | 全部功能 |

### API 配置

普通 τ 任务可以使用仓库根目录的 <code>.env</code>：

~~~bash
cp .env.example .env
~~~

REAL-Bench 的完整评测推荐使用共享配置目录（默认 <code>~/.config/qwaudio</code>）：

| 文件 | 用途 |
| --- | --- |
| <code>config.env</code> | 供应商的 API key、地址和原始模型名 |
| <code>qw.env</code> | 被测 Agent 后端的连接选择 |
| <code>realbench.env</code> | 用户模拟器、ASR/TTS、评分和题目生成角色 |

配置示例和角色继承规则见 [API 配置说明](docs/api-configuration.md)。私有
配置文件不要提交到 Git；运行结果只记录非敏感连接信息和引用名。

### 运行一个最小语音 smoke

下面的命令运行一通 Gaia2 语音电话并保存结果。它会为该通话创建独立的 Agent
容器和应用状态：

~~~bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1 \
  --max-concurrency 1 \
  --save-to realbench-smoke
~~~

使用 <code>--dry-run</code> 只查看运行计划；使用 <code>--task-ids 0</code> 固定任务；使用
<code>--resume</code> 继续未完成的批次。结果写入
<code>data/simulations/realbench-smoke/</code>。

运行 Gaia2 文本任务的示例：

~~~bash
UV_FROZEN=1 uv run tau2 run \
  --domain gaia2 \
  --agent-llm gpt-4.1 \
  --user-llm gpt-4.1 \
  --num-trials 1 \
  --num-tasks 2 \
  --save-to gaia2-text-smoke
~~~

### 查看和重算结果

~~~bash
UV_FROZEN=1 uv run tau2 view --file data/simulations/realbench-smoke

# 对已有运行补算 REAL 指标
UV_FROZEN=1 uv run tau2 metrics run data/simulations/realbench-smoke \
  --config examples/real-metrics.unified.yaml
UV_FROZEN=1 uv run tau2 metrics summarize data/simulations/realbench-smoke
UV_FROZEN=1 uv run tau2 metrics export data/simulations/realbench-smoke \
  --output realbench-metrics.json
~~~

<code>metrics run</code> 会复用已经完成的评分请求，只评估缺失或失败的项目。修改模型、
提示词、指标版本或证据后会生成新的 content-addressed profile，历史结果不会
被覆盖。

## Gaia2 数据和验证

Gaia2 的本地任务位于 <code>data/tau2/domains/gaia2/</code>。首次导入原始数据和资源后，
执行：

~~~bash
UV_FROZEN=1 uv run tau2 gaia2 import \
  --source /path/to/gaia2 \
  --assets /path/to/gaia2_filesystem

UV_FROZEN=1 uv run tau2 gaia2 verify \
  --reference \
  --workers 8 \
  --seal
~~~

运行时代码、任务或资源发生变化后，必须重新验证并 seal。验证使用本地原生工具
和严格 replay，不调用模型；seal 只表示数据和运行时已验证，不代表 Agent 已经
取得高分。更多任务布局见 [Gaia2 数据说明](data/tau2/domains/gaia2/README.md)
和 [Gaia2 指南](docs/gaia2.md)。

## REAL 指标

默认示例配置为 [examples/real-metrics.unified.yaml](examples/real-metrics.unified.yaml)，
当前可选 measure 包括：

| measure | 内容 |
| --- | --- |
| <code>intent</code> | 用户需求与 Agent 顶层任务的 Precision、Recall、F1、覆盖和来源证据 |
| <code>execution</code> | 工具选择、参数、调用顺序和业务状态效果 |
| <code>voice</code> | 响应延迟、响应率、打断、重叠、按时率和语音前置发言 |
| <code>interaction</code> | 交互时序、让步、响应和有效发言行为 |
| <code>state_interaction</code> | 多阶段任务中的状态变化与交互状态 |
| <code>conciseness</code> | 必要信息覆盖、回复长度和反馈质量 |

REAL 指标的定义、证据目录、缓存规则和离线验证见
[REAL-derived diagnostics](docs/real-metrics.md)。指标源文件、上游版本和哈希
记录在 [src/tau2/diagnostics/SOURCE.json](src/tau2/diagnostics/SOURCE.json)。

## Qwen Audio Agent 集成

集成代码位于 [integrations/qwen_audio_agent/](integrations/qwen_audio_agent/)。
它为每个 trial 隔离容器、workspace、MCP、控制连接和应用状态；Agent 只能看到
明确注入的策略与工具 schema，任务答案、原始数据库和评分证据留在评测端。

构建 Qwen Agent 镜像：

~~~bash
UV_FROZEN=1 uv run python integrations/qwen_audio_agent/build_image.py \
  /path/to/qwen-audio-agent
~~~

集成运行、语音前端、场景时钟、隔离边界和验证命令见
[Qwen Audio Agent README](integrations/qwen_audio_agent/README.md)。实时服务测试
需要对应的模型、ASR、TTS 和 Docker 环境，并会产生服务调用费用。

## 开发和测试

~~~bash
UV_FROZEN=1 uv run pytest tests/ \
  --ignore=tests/test_voice \
  --ignore=tests/test_streaming \
  --ignore=tests/test_domains/test_banking_knowledge

UV_FROZEN=1 uv run pytest tests/test_diagnostics
UV_FROZEN=1 uv run ruff check .
UV_FROZEN=1 uv run ruff format --check .
~~~

对应的 Make 目标见 [Makefile](Makefile)：<code>make test</code>、<code>make test-diagnostics</code>、
<code>make test-voice</code>、<code>make test-gaia2</code>、<code>make test-all</code> 和
<code>make check-all</code>。

## 目录结构

~~~text
src/tau2/                         评测运行时、Agent、环境、指标和 CLI
data/tau2/domains/                任务、策略、数据库和语言数据
integrations/qwen_audio_agent/    Qwen Audio Agent 容器与 smoke 入口
examples/                         REAL 指标和运行配置示例
docs/                             评测、配置、Gaia2 和指标说明
tests/                            核心、语音、Gaia2 和诊断测试
data/simulations/                 本地运行结果（默认不纳入版本控制）
~~~

## 相关文档

- [Getting Started](docs/getting-started.md)：安装、API key 和基础运行
- [API 配置](docs/api-configuration.md)：<code>config.env</code>、<code>qw.env</code>、<code>realbench.env</code>
- [REAL 指标](docs/real-metrics.md)：评分定义、证据和缓存
- [Gaia2 指南](docs/gaia2.md)：数据导入、验证和运行
- [Qwen Audio Agent](integrations/qwen_audio_agent/README.md)：完整语音 Agent 集成
- [CLI Reference](docs/cli-reference.md)：所有 <code>tau2</code> 命令和参数
- [指标清单](docs/metrics-inventory.md)：当前指标版本和来源

## 许可证与来源

项目代码使用 [MIT License](LICENSE)。REAL 指标中引用的上游 REAL-Bench/ARE
实现、版本和文件哈希见 [src/tau2/diagnostics/SOURCE.json](src/tau2/diagnostics/SOURCE.json)。
Gaia2 数据、模型服务和第三方语音服务分别遵循其原始许可证与使用条款。

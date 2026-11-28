> 当前仓库只提供 Gaia2 运行入口。旧 τ-bench 域和多语言矩阵不属于新运行路径。

# Getting Started

需要 Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)，以及运行
Qwen Audio Agent 时所需的 Docker 和语音系统依赖。

```bash
git clone https://github.com/Liu-Chaolei/REAL-Bench.git
cd REAL-Bench
UV_FROZEN=1 uv sync --extra dev
UV_FROZEN=1 uv run tau2 check-data
```

语音和诊断运行使用对应 extras：

```bash
UV_FROZEN=1 uv sync --extra voice --extra diagnostics --extra dev
```

文本 smoke：

```bash
UV_FROZEN=1 uv run tau2 run \
  --domain gaia2 \
  --agent-llm gpt-4.1 \
  --user-llm gpt-4.1 \
  --num-tasks 1 \
  --save-to gaia2-text-smoke
```

语音 smoke：

```bash
integrations/qwen_audio_agent/run_hpc.sh smoke \
  --domain gaia2 \
  --config integrations/qwen_audio_agent/config.gaia2.unified.json \
  --metrics-config examples/real-metrics.unified.yaml \
  --num-tasks 1 --max-concurrency 1
```

Qwen 的供应商连接、Agent backend 和评测角色分别由 `config.env`、`qw.env` 和
`realbench.env` 提供。私有配置不要提交到 Git。完整配置说明见
[API configuration](api-configuration.md)，Qwen 容器说明见
[integration guide](../integrations/qwen_audio_agent/README.md)。

任务和资源位于 `data/tau2/domains/gaia2/`。导入或修改后运行：

```bash
UV_FROZEN=1 uv run tau2 gaia2 verify --reference --workers 8 --seal
```

结果写入 `data/results/`。使用 `tau2 view` 查看结果，使用
`tau2 metrics run`、`summarize` 和 `export` 处理 REAL diagnostics。

开发检查：

```bash
make test
make test-diagnostics
make check-all
```

更多信息见 [Gaia2 guide](gaia2.md)、[running the benchmark](running-the-benchmark.md)、
[CLI reference](cli-reference.md) 和 [evaluation](evaluation.md)。

#!/usr/bin/env bash
set -euo pipefail

SERIES_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SERIES_REPO_ROOT"
SERIES_DATASET="${1:-data/tau2/task_series/gaia2/v1-full}"
SERIES_CONFIG="${2:-docs/gaia2-series.config.example.json}"
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export UV_FROZEN=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
export PYTHONUNBUFFERED=1
export LOGURU_LEVEL=INFO
export TAU2_DATA_DIR="${TAU2_DATA_DIR:-$SERIES_REPO_ROOT/data}"
if [[ -d "$SERIES_REPO_ROOT/.venv/lib/tau-native" ]]; then
    export LD_LIBRARY_PATH="$SERIES_REPO_ROOT/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

series_finish() {
    SERIES_EXIT_CODE=$?
    trap - EXIT
    printf '%s\n' "$SERIES_EXIT_CODE" > "$SERIES_DATASET/full.exit"
    exit "$SERIES_EXIT_CODE"
}
trap series_finish EXIT
printf 'running\n' > "$SERIES_DATASET/full.exit"
SERIES_POOL_ARGS=()
SERIES_SCALING_ARGS=()
if [[ "${SERIES_FIXED_CONCURRENCY:-0}" == "1" ]]; then
    SERIES_SCALING_ARGS=(--fixed-concurrency)
fi
if [[ -n "${SERIES_API_KEY_POOL:-}" ]]; then
    read -r -a SERIES_POOL_KEYS <<< "$SERIES_API_KEY_POOL"
    SERIES_POOL_ARGS=(--api-key-pool "${SERIES_POOL_KEYS[@]}" --per-key-concurrency "${SERIES_PER_KEY_CONCURRENCY:-6}")
fi
"$SERIES_REPO_ROOT/.venv/bin/python" -m tau2.task_authoring.gaia2_series.cli full \
    --dataset "$SERIES_DATASET" --config "$SERIES_CONFIG" \
    --start-concurrency "${SERIES_START_CONCURRENCY:-8}" \
    --max-concurrency "${SERIES_MAX_CONCURRENCY:-16}" \
    --request-timeout 600 --provider-attempts 2 "${SERIES_POOL_ARGS[@]}" "${SERIES_SCALING_ARGS[@]}"

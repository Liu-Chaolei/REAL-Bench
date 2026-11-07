#!/usr/bin/env bash
# Resumable offline authoring pilot. No agent evaluation or data publication.
set -euo pipefail

SERIES_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SERIES_REPO_ROOT"
SERIES_DATASET="${1:-data/tau2/task_series/gaia2/v2-asr-hotwords}"
SERIES_CONFIG="${2:-docs/gaia2-series.config.example.json}"
SERIES_PYTHON="$SERIES_REPO_ROOT/.venv/bin/python"
SERIES_CONCURRENCY="${SERIES_CONCURRENCY:-1}"
SERIES_REQUEST_TIMEOUT="${SERIES_REQUEST_TIMEOUT:-600}"
SERIES_PROVIDER_ATTEMPTS="${SERIES_PROVIDER_ATTEMPTS:-2}"
SERIES_PILOT_PER_CATEGORY="${SERIES_PILOT_PER_CATEGORY:-1}"
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
if [[ "$SERIES_PILOT_PER_CATEGORY" -gt 0 ]]; then
    SERIES_SCOPE=(--pilot-per-category "$SERIES_PILOT_PER_CATEGORY")
else
    SERIES_SCOPE=(--pilot)
fi
export UV_FROZEN=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
if [[ -d "$SERIES_REPO_ROOT/.venv/lib/tau-native" ]]; then
    export LD_LIBRARY_PATH="$SERIES_REPO_ROOT/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

if [[ ! -f "$SERIES_DATASET/manifest.json" ]]; then
    "$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli inventory --output "$SERIES_DATASET"
fi

series_finish() {
    SERIES_EXIT_CODE=$?
    trap - EXIT
    printf '%s\n' "$SERIES_EXIT_CODE" > "$SERIES_DATASET/pilot.exit"
    "$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli review-sheet \
        --dataset "$SERIES_DATASET" "${SERIES_SCOPE[@]}" || true
    "$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli status \
        --dataset "$SERIES_DATASET" || true
    printf 'Pilot exit code: %s. Human review is pending; no release was published.\n' "$SERIES_EXIT_CODE"
    exit "$SERIES_EXIT_CODE"
}
trap series_finish EXIT
printf 'running\n' > "$SERIES_DATASET/pilot.exit"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SERIES_DATASET/pilot.started"

set +e
"$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli generate \
    --dataset "$SERIES_DATASET" --config "$SERIES_CONFIG" "${SERIES_SCOPE[@]}" \
    --concurrency "$SERIES_CONCURRENCY" --request-timeout "$SERIES_REQUEST_TIMEOUT" \
    --provider-attempts "$SERIES_PROVIDER_ATTEMPTS"
SERIES_GENERATION_EXIT=$?
"$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli verify \
    --dataset "$SERIES_DATASET" "${SERIES_SCOPE[@]}"
SERIES_VERIFICATION_EXIT=$?
"$SERIES_PYTHON" -m tau2.task_authoring.gaia2_series.cli review \
    --dataset "$SERIES_DATASET" --config "$SERIES_CONFIG" "${SERIES_SCOPE[@]}" \
    --concurrency "$SERIES_CONCURRENCY" --request-timeout "$SERIES_REQUEST_TIMEOUT" \
    --provider-attempts "$SERIES_PROVIDER_ATTEMPTS"
SERIES_REVIEW_EXIT=$?
if (( SERIES_GENERATION_EXIT || SERIES_VERIFICATION_EXIT || SERIES_REVIEW_EXIT )); then
    exit 1
fi
exit 0

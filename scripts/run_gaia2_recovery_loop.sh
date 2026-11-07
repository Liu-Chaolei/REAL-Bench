#!/usr/bin/env bash
set -euo pipefail

DATASET=${1:?dataset required}
CONFIG=${2:?config required}
WAIT_PID=${3:-}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export PYTHONUNBUFFERED=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
export TAU2_DATA_DIR="$ROOT/data"
export PYTHONPATH="$ROOT/data/simulations/gaia2-series-mimo-20261028/runtime-key-quota-isolation/src"
export LD_LIBRARY_PATH="$ROOT/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
LOG="$DATASET/recovery-loop.log"

if [[ -n "$WAIT_PID" ]]; then
  while kill -0 "$WAIT_PID" 2>/dev/null; do sleep 30; done
fi

for pass in 1 2 3 4 5 6; do
  echo "$(date -Is) starting recovery pass $pass" >> "$LOG"
  "$ROOT/.venv/bin/python" -m tau2.task_authoring.gaia2_series.cli recover \
    --dataset "$DATASET" --config "$CONFIG" --repair-rounds 1 \
    --concurrency 36 --request-timeout 600 --provider-attempts 2 \
    --api-key-pool MIMO_API_KEY1 MIMO_API_KEY3 MIMO_API_KEY4 \
    --per-key-concurrency 12 --fixed-concurrency >> "$LOG" 2>&1 || true
  status=$(DATASET_PATH="$DATASET" "$ROOT/.venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path
p = Path(os.environ["DATASET_PATH"]) / "recovery-run.json"
try:
    state = json.loads(p.read_text())
    status = state.get("status", "unknown")
    if status == "needs_attention" and not state.get("made_progress", False):
        print("no_progress")
    else:
        print(status)
except Exception:
    print("unknown")
PY
  )
  echo "$(date -Is) recovery pass $pass status=$status" >> "$LOG"
  [[ "$status" == "complete" ]] && exit 0
  if [[ "$status" != "needs_attention" ]]; then
    echo "$(date -Is) stopping recovery loop: $status; inspect recovery-run.json" >> "$LOG"
    exit 2
  fi
  sleep 10
done

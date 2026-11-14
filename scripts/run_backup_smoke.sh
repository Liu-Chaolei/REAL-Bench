#!/usr/bin/env bash
# Keep a copied checkout's host-side outputs, caches and temporary files local.
set -euo pipefail

TAU_BACKUP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$TAU_BACKUP_ROOT"

export PYTHONNOUSERSITE=True
export PYTHONDONTWRITEBYTECODE=1
export UV_FROZEN=1
export LITELLM_LOCAL_MODEL_COST_MAP=True
export TAU2_DATA_DIR="$TAU_BACKUP_ROOT/data"
export TMPDIR="$TAU_BACKUP_ROOT/.cache/tmp"
export XDG_CACHE_HOME="$TAU_BACKUP_ROOT/.cache"
export HF_HOME="$TAU_BACKUP_ROOT/.cache/huggingface"
export TORCH_HOME="$TAU_BACKUP_ROOT/.cache/torch"
export NUMBA_CACHE_DIR="$TAU_BACKUP_ROOT/.cache/numba"
export MPLCONFIGDIR="$TAU_BACKUP_ROOT/.cache/matplotlib"
mkdir -p "$TMPDIR" "$HF_HOME" "$TORCH_HOME" "$NUMBA_CACHE_DIR" "$MPLCONFIGDIR"

# Keep future backup smoke runs bounded at 1200 simulated seconds by default:
# with the 200 ms tick this is 6000 full-duplex ticks. An explicit
# --max-seconds (including 0 for unlimited) still takes precedence.
has_max_seconds=0
for smoke_arg in "$@"; do
    if [[ "$smoke_arg" == "--max-seconds" || "$smoke_arg" == --max-seconds=* ]]; then
        has_max_seconds=1
        break
    fi
done
if [[ "$has_max_seconds" -eq 0 ]]; then
    set -- "$@" --max-seconds 1200
fi

if [[ -d "$TAU_BACKUP_ROOT/.venv/lib/tau-native" ]]; then
    export LD_LIBRARY_PATH="$TAU_BACKUP_ROOT/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

"$TAU_BACKUP_ROOT/.venv/bin/python" - <<'PY'
from pathlib import Path
import tau2
from tau2.utils.utils import DATA_DIR

root = Path.cwd().resolve()
if not Path(tau2.__file__).resolve().is_relative_to(root):
    raise SystemExit("Copied venv imports another checkout; repair its editable .pth first")
if DATA_DIR.resolve() != root / "data":
    raise SystemExit("Data directory does not belong to this checkout")
PY

exec "$TAU_BACKUP_ROOT/integrations/qwen_audio_agent/run_hpc.sh" smoke "$@"

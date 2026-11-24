#!/usr/bin/env bash
# Run on the platform Docker host with the independent evaluation venv.
set -euo pipefail
TAU_EVAL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONNOUSERSITE=True
export UV_FROZEN=1
if [[ -d "$TAU_EVAL_ROOT/.venv/lib/tau-native" ]]; then
    export LD_LIBRARY_PATH="$TAU_EVAL_ROOT/.venv/lib/tau-native${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
if [[ ! -x "$TAU_EVAL_ROOT/.venv/bin/tau2" ]]; then
    echo 'Install the Python 3.12 voice/dev venv described in README.md first.' >&2
    exit 1
fi
if [[ "${1:-}" == "smoke" ]]; then
    shift
    exec "$TAU_EVAL_ROOT/.venv/bin/python" "$TAU_EVAL_ROOT/integrations/qwen_audio_agent/smoke.py" "$@"
fi
exec "$TAU_EVAL_ROOT/.venv/bin/tau2" "$@"

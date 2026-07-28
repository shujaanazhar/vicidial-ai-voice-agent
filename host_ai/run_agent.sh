#!/usr/bin/env bash
# run_agent.sh — launch the AI voice agent with the environment CTranslate2 needs.
#
# Why this wrapper exists: faster-whisper's CUDA backend (CTranslate2) will not
# find cuDNN 9.x unless LD_LIBRARY_PATH points at the wheels' bundled nvidia
# libs. Without it you get a cuDNN load failure at model init, not at import,
# so it looks like a GPU problem rather than a path problem.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

NVIDIA_LIBS="$(echo "$VENV"/lib/python3.12/site-packages/nvidia/*/lib | tr ' ' ':')"
export LD_LIBRARY_PATH="${NVIDIA_LIBS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

exec "$VENV/bin/python" "$HERE/ai_agent.py" "$@"

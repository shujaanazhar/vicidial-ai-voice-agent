#!/usr/bin/env bash
# setup_host_ai.sh — install the fully-local $0 AI voice stack on this host.
# Stack: faster-whisper (STT, GPU) + Ollama (LLM, GPU) + Piper (TTS, CPU).
# Safe to re-run. Run in parallel with the VM build — nothing here needs the VM.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HERE/host_ai/.venv"
PIPER_DIR="$HERE/host_ai/piper_voices"
# llama3.2:3b is fast and leaves VRAM for whisper. Set qwen2.5:7b for quality.
LLM_MODEL="${LLM_MODEL:-llama3.2:3b}"

echo "== GPU =="
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader \
  || echo "!! No GPU visible — local stack will fall back to CPU (slow)."

# 1) Ollama (LLM runtime) — installs to /usr/local, needs sudo internally.
if ! command -v ollama >/dev/null 2>&1; then
  echo "== Installing Ollama =="
  curl -fsSL https://ollama.com/install.sh | sh
fi
echo "== Pulling LLM: $LLM_MODEL =="
ollama pull "$LLM_MODEL"

# 2) Python venv + STT/TTS libs
echo "== Python venv @ $VENV =="
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$HERE/requirements.txt"

# 3) Piper voice
echo "== Piper voice (en_US-lessac-medium) =="
mkdir -p "$PIPER_DIR"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
for f in en_US-lessac-medium.onnx en_US-lessac-medium.onnx.json; do
  [ -f "$PIPER_DIR/$f" ] || wget -q --show-progress -O "$PIPER_DIR/$f" "$BASE/$f"
done

echo
echo "Done.  LLM=$LLM_MODEL  venv=$VENV  piper=$PIPER_DIR"
echo "Smoke-test the LLM:  ollama run $LLM_MODEL 'say hi in 3 words'"

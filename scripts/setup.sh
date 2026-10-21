#!/usr/bin/env bash
#
# Set up a local environment for the voice agent.
#
#   ./scripts/setup.sh
#
# Creates .venv, installs the dependencies and downloads the Whisper model.
# The model is 141 MB, so it is downloaded instead of being stored in the
# repository.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
MODEL="$ROOT/ggml-base.en.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"

echo "==> Python environment"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r requirements.txt

echo "==> Whisper model"
if [ -f "$MODEL" ]; then
  echo "    already present: $MODEL"
else
  echo "    downloading $MODEL_URL"
  curl -fL --progress-bar -o "$MODEL.part" "$MODEL_URL"
  mv "$MODEL.part" "$MODEL"
fi

echo "==> Checking the local tooling"
for tool in ffmpeg node; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "    $tool: $(command -v "$tool")"
  else
    echo "    WARNING: $tool is not installed (macOS: brew install ffmpeg node)"
  fi
done
if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null 2>&1; then
  echo "    WARNING: Ollama was not found. The n8n workflow needs it for general questions."
fi

cat <<'EOF'

Done. Next steps:

  cp .env.example .env      # add your n8n API key
  ./scripts/test_all.sh      # run the test suites
  .venv/bin/python voice_agent.py

EOF

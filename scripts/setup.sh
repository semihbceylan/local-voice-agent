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
MISSING=""

check_tool() {
  local name="$1" hint="$2" path="${3:-}"
  local found=""
  if [ -n "$path" ]; then
    [ -x "$path" ] && found="$path"
  else
    found="$(command -v "$name" 2>/dev/null || true)"
  fi
  if [ -n "$found" ]; then
    echo "    $name: $found"
  else
    echo "    MISSING: $name ($hint)"
    MISSING="$MISSING $name"
  fi
}

check_tool ffmpeg "macOS: brew install ffmpeg"
check_tool curl "needed to download the model"
check_tool node "needed for the parser tests, macOS: brew install node"
check_tool whisper-cli "whisper.cpp, needed for the speech recognition" \
  "${WHISPER_CLI:-/opt/homebrew/bin/whisper-cli}"

if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null 2>&1; then
  # Only the n8n workflow needs Ollama, the client and the tests do not.
  echo "    WARNING: ollama was not found. The n8n workflow needs it for general questions."
fi

cat <<'EOF'

Done. Next steps:

  cp .env.example .env      # add your n8n API key
  ./scripts/test_all.sh      # run the test suites
  .venv/bin/python voice_agent.py

EOF

if [ -n "$MISSING" ]; then
  echo "Missing tools:$MISSING" >&2
  echo "The voice agent needs ffmpeg, curl and whisper-cli; the test suite" >&2
  echo "additionally needs node. See docs/troubleshooting.md." >&2
  exit 1
fi

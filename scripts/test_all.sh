#!/usr/bin/env bash
#
# Run every test suite that does not need a microphone.
#
#   ./scripts/test_all.sh
#
# The integration suite needs a running n8n instance and an API key in
# N8N_API_KEY; it is skipped when they are missing.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

FAILED=0

run() {
  local name="$1"; shift
  echo
  echo "=================================================================="
  echo "  $name"
  echo "=================================================================="
  if "$@"; then
    echo "  -> $name OK"
  else
    echo "  -> $name FAILED"
    FAILED=1
  fi
}

run "order parser (node --test)"        node --test tests/parser/
run "voice activity detection"          "$PY" tests/test_vad.py
run "call mode"                         "$PY" tests/test_call_mode.py
run "farewell detection"                "$PY" tests/test_farewell.py
run "workflow build guard"              "$PY" tests/test_workflow_build.py
run "speech recognition reliability"    "$PY" tests/stt_reliability.py

if [ -n "${N8N_API_KEY:-}" ]; then
  run "integration (isolated workflows)"  "$PY" tests/test_integration.py --deploy
else
  echo
  echo "  (skipping the integration suite: N8N_API_KEY is not set)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "All suites passed."
else
  echo "Some suites failed."
fi
exit "$FAILED"

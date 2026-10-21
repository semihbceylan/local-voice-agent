#!/usr/bin/env bash
#
# Build the n8n workflow and deploy it to the n8n instance.
#
#   export N8N_API_KEY=...          # never put the key in a file
#   ./scripts/deploy_workflow.sh    # update the existing workflow
#   ./scripts/deploy_workflow.sh --new   # create a second workflow
#
# The key is only read from the environment.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BASE_URL="${N8N_BASE_URL:-http://localhost:5678}"
WEBHOOK_PATH="${N8N_WEBHOOK_PATH:-voice-agent}"
WORKFLOW_ID="${N8N_WORKFLOW_ID:-nPLoCe6uuit1oMqs}"
OUT="$ROOT/workflows/voice_agent_production.json"

if [ -z "${N8N_API_KEY:-}" ]; then
  echo "N8N_API_KEY is not set." >&2
  echo "Create a key in n8n (Settings -> n8n API) and export it:" >&2
  echo "  export N8N_API_KEY=n8n_api_..." >&2
  exit 1
fi

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "==> Building the workflow"
"$PY" build_voice_workflow.py --webhook-path "$WEBHOOK_PATH" --out "$OUT"

echo "==> Deploying to $BASE_URL"
if [ "${1:-}" = "--new" ]; then
  curl -sS -X POST "$BASE_URL/api/v1/workflows" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" -H 'Content-Type: application/json' \
    --data-binary @"$OUT" \
    | "$PY" -c 'import json,sys; w=json.load(sys.stdin); print("created workflow", w.get("id"), w.get("name"))'
else
  curl -sS -X PUT "$BASE_URL/api/v1/workflows/$WORKFLOW_ID" \
    -H "X-N8N-API-KEY: $N8N_API-KEY" -H 'Content-Type: application/json' \
    --data-binary @"$OUT" \
    | "$PY" -c 'import json,sys; w=json.load(sys.stdin); print("updated workflow", w.get("id"), "active:", w.get("active"))'
fi

echo
echo "Done. Smoke test:"
echo "  curl -s -X POST $BASE_URL/webhook/$WEBHOOK_PATH -H 'Content-Type: application/json' \\"
echo "    -d '{\"chatInput\":\"Where is my order 1001?\",\"sessionId\":\"t\"}'"

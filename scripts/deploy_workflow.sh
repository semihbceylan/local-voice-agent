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
WORKFLOW_ID="${N8N_WORKFLOW_ID:-}"
OUT="$ROOT/workflows/voice_agent_production.json"

if [ -z "${N8N_API_KEY:-}" ]; then
  echo "N8N_API_KEY is not set." >&2
  echo "Create a key in n8n (Settings -> n8n API) and export it:" >&2
  echo "  export N8N_API_KEY=n8n_api_..." >&2
  exit 1
fi

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# request <method> <url>: prints the body, fails on a HTTP error
request() {
  local method="$1" url="$2" body status
  body="$(mktemp)"
  status="$(curl -sS -o "$body" -w '%{http_code}' \
    -X "$method" "$url" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    -H 'Content-Type: application/json' \
    --data-binary @"$OUT")" || {
      echo "Request to $url failed." >&2
      rm -f "$body"
      exit 1
    }
  if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    echo "n8n answered with HTTP $status:" >&2
    cat "$body" >&2
    echo >&2
    rm -f "$body"
    exit 1
  fi
  cat "$body"
  rm -f "$body"
}

# The webhook id has to stay the same, otherwise n8n registers a new webhook
# and the committed workflow drifts away from the deployed one.
WEBHOOK_ID=""
if [ -f "$OUT" ]; then
  WEBHOOK_ID="$("$PY" "$ROOT/scripts/webhook_id.py" "$OUT")"
fi

echo "==> Building the workflow"
if [ -n "$WEBHOOK_ID" ]; then
  "$PY" build_voice_workflow.py --webhook-path "$WEBHOOK_PATH" \
    --webhook-id "$WEBHOOK_ID" --out "$OUT"
else
  "$PY" build_voice_workflow.py --webhook-path "$WEBHOOK_PATH" --out "$OUT"
fi

echo "==> Checking $BASE_URL"
if ! curl -sS -o /dev/null --max-time 10 "$BASE_URL/healthz"; then
  echo "n8n is not reachable at $BASE_URL." >&2
  echo "Start it, or set N8N_BASE_URL to the right address." >&2
  exit 1
fi

echo "==> Deploying to $BASE_URL"
if [ "${1:-}" = "--new" ]; then
  RESPONSE="$(request POST "$BASE_URL/api/v1/workflows")"
  printf '%s' "$RESPONSE" | "$PY" -c '
import json, sys
workflow = json.load(sys.stdin)
if not workflow.get("id"):
    raise SystemExit("n8n did not return a workflow id")
print("created workflow", workflow["id"], workflow.get("name", ""))
print("activate it in the n8n UI before the webhook answers")
'
else
  if [ -z "$WORKFLOW_ID" ]; then
    echo "N8N_WORKFLOW_ID is not set (or use --new to create a workflow)." >&2
    exit 1
  fi
  RESPONSE="$(request PUT "$BASE_URL/api/v1/workflows/$WORKFLOW_ID")"
  printf '%s' "$RESPONSE" | "$PY" -c '
import json, sys
workflow = json.load(sys.stdin)
if not workflow.get("id"):
    raise SystemExit("n8n did not return a workflow id")
print("updated workflow", workflow["id"], "active:", workflow.get("active"))
'
  ACTIVE="$(curl -sS --max-time 10 "$BASE_URL/api/v1/workflows/$WORKFLOW_ID" \
    -H "X-N8N-API-KEY: $N8N_API_KEY" \
    | "$PY" -c 'import json,sys; print(bool(json.load(sys.stdin).get("active")))')"
  if [ "$ACTIVE" != "True" ]; then
    echo "    the workflow is inactive, activating it"
    request POST "$BASE_URL/api/v1/workflows/$WORKFLOW_ID/activate" >/dev/null
    echo "    activated"
  fi
fi

echo
echo "==> Checking that the webhook answers"
# n8n re-registers the webhook path shortly after an update, so give it a
# moment before declaring the deployment broken.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sS -o /dev/null --max-time 10 -X POST \
       "$BASE_URL/webhook/$WEBHOOK_PATH" \
       -H 'Content-Type: application/json' \
       -d '{"chatInput":"Where is my order 1001?","sessionId":"deploy-check"}'; then
    echo "    the webhook answers"
    break
  fi
  if [ "$attempt" = "10" ]; then
    echo "    WARNING: the webhook does not answer yet. Check that the" >&2
    echo "    workflow is active and that the path is '$WEBHOOK_PATH'." >&2
  fi
  sleep 2
done

echo
echo "Done. Smoke test:"
echo "  curl -s -X POST $BASE_URL/webhook/$WEBHOOK_PATH -H 'Content-Type: application/json' \\"
echo "    -d '{\"chatInput\":\"Where is my order 1001?\",\"sessionId\":\"t\"}'"

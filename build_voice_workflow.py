#!/usr/bin/env python3
"""
Build the DemoShop Voice Agent n8n workflow JSON.

Deterministic order lookups use the native Data Table node. Only general
questions reach the LLM. Secrets are never embedded in the generated JSON.

Usage:
    python3 build_voice_workflow.py --webhook-path voice-agent-test --out test.json
    python3 build_voice_workflow.py --parser-test --webhook-path parser-test --out parser.json
"""

import argparse
import json
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PARSER_SOURCE = BASE_DIR / "workflow" / "extract_order_id.js"

def _env(name: str, default: str) -> str:
    import os

    value = os.environ.get(name)
    return value if value not in (None, "") else default


# Identifiers of the n8n instance this workflow is built for. They are not
# secrets, but they are instance specific, so they can be overridden.
TABLE_ID = _env("N8N_DATA_TABLE_ID", "RavtVTN9w9NPZFuZ")
OLLAMA_CREDENTIAL = {
    "ollamaApi": {
        "id": _env("N8N_OLLAMA_CREDENTIAL_ID", "VizCi15LZHPUuqzo"),
        "name": "Ollama account",
    }
}


def load_parser_source() -> str:
    """The order parser lives in a single file and is embedded into the workflow."""
    if not PARSER_SOURCE.exists():
        raise SystemExit(f"parser source missing: {PARSER_SOURCE}")
    return PARSER_SOURCE.read_text(encoding="utf-8")

NORMALIZE_INPUT_CODE = r"""
// Normalize and validate the webhook payload. Always returns one item.
const out = [];
for (const item of $input.all()) {
  const body = item.json && item.json.body && typeof item.json.body === 'object'
    ? item.json.body
    : {};

  let chatInput = typeof body.chatInput === 'string' ? body.chatInput : '';
  chatInput = chatInput.replace(/\s+/g, ' ').trim().slice(0, 1000);

  let sessionId = typeof body.sessionId === 'string' ? body.sessionId.trim() : '';
  sessionId = sessionId.slice(0, 128) || 'anonymous';

  out.push({ json: { chatInput, sessionId } });
}
return out;
"""

EXTRACT_ORDER_ID_CODE = load_parser_source()
FORMAT_CLARIFICATION_CODE = r"""
// Deterministic response for empty, ambiguous or uncertain input.
// The parser supplies the wording, so the client and the parser agree.
const item = $input.first();
const route = String(item.json.route || '');
const sessionId = String(item.json.sessionId || 'anonymous');

let response = item.json.clarification;
if (!response) {
  if (route === 'ambiguous') {
    response = 'Which order number would you like me to check?';
  } else {
    response = "I didn't catch a question. Please tell me your order number or ask a support question.";
  }
}

return [{ json: { response, sessionId } }];
"""

FORMAT_ORDER_RESULT_CODE = r"""
// Runs after the native Data Table lookup. alwaysOutputData guarantees an item
// even when the order does not exist, so the requested ID is never lost.
const source = $('Extract Order ID').first().json;
const orderId = String(source.orderId || '');
const sessionId = String(source.sessionId || 'anonymous');
const confidence = String(source.confidence || 'exact');

// Always Output Data emits a dummy success item even when the lookup fails,
// so this branch would otherwise race the error branch and could answer
// "not found" for a database error. Suppress it when the error output fired.
try {
  const lookupErrors = $('Lookup Order').all(1);
  if (lookupErrors.some((item) => item && item.json && item.json.error)) {
    return [];
  }
} catch (error) {
  // No error branch recorded; continue with normal formatting.
}

const rows = $input.all()
  .map((item) => item.json || {})
  .filter((row) => row && Object.keys(row).length > 0 &&
    String(row.order_id == null ? '' : row.order_id) === orderId);

if (rows.length === 0) {
  // A confident miss and an uncertain interpretation are answered
  // differently: "recovered" means the number was reconstructed from a
  // compound phrase, so the customer is asked to confirm it.
  const response = confidence === 'exact'
    ? `I couldn't find order #${orderId}. Please check the order number.`
    : `I couldn't find order #${orderId}. Could you confirm the order number?`;
  return [{ json: { response, sessionId } }];
}

const order = rows[0];
const status = String(order.status == null ? '' : order.status).trim();
const product = String(order.product == null ? '' : order.product).trim();
let tracking = String(order.tracking_number == null ? '' : order.tracking_number).trim();
if (/^(empty|null|none|n\/a|-)$/i.test(tracking)) tracking = '';

function statusPhrase(value) {
  switch (value.toLowerCase()) {
    case 'shipped': return 'has shipped';
    case 'processing': return 'is being processed';
    case 'delivered': return 'has been delivered';
    case 'cancelled':
    case 'canceled': return 'was cancelled';
    case 'out for delivery': return 'is out for delivery';
    case 'returned': return 'was returned';
    default: return value ? `is ${value}` : '';
  }
}

// Keep spoken answers short: playback duration dominates total latency.
const parts = [];
const phrase = statusPhrase(status);
parts.push(phrase
  ? `Order ${orderId} ${phrase}.`
  : `I found order ${orderId}, but its status is currently unavailable.`);

const details = [];
if (product) details.push(`It's the ${product}`);
if (tracking) details.push(`tracking number ${tracking}`);
if (details.length) {
  parts.push(`${details.join(', ')}.`);
}
if (!tracking) {
  parts.push(status.toLowerCase() === 'processing'
    ? 'No tracking number is available yet.'
    : 'There is no tracking number on file.');
}

return [{ json: { response: parts.join(' '), sessionId } }];
"""

FORMAT_LOOKUP_ERROR_CODE = r"""
// Connected to the Data Table node's error output. A database failure must
// never be reported as "order not found".
const source = $('Extract Order ID').first().json;
const sessionId = String(source.sessionId || 'anonymous');

return [{ json: {
  response: "I'm having trouble accessing order information right now. Please try again shortly.",
  sessionId
} }];
"""

FORMAT_SUGGEST_ORDER_CODE = r"""
// Runs when the order was not found. A single misheard digit is common
// ("1001" heard as "1011"), so offer the closest known order as a question.
// Nothing is ever returned silently: the customer confirms.
const source = $('Extract Order ID').first().json;
const orderId = String(source.orderId || '');
const sessionId = String(source.sessionId || 'anonymous');

// A lookup error must never be reported as "not found".
try {
  const lookupErrors = $('Lookup Order').all(1);
  if (lookupErrors.some((item) => item && item.json && item.json.error)) {
    return [];
  }
} catch (error) {
  // No error branch recorded; continue.
}

function distance(a, b) {
  const rows = a.length + 1;
  const cols = b.length + 1;
  let previous = new Array(cols);
  for (let j = 0; j < cols; j++) previous[j] = j;
  for (let i = 1; i < rows; i++) {
    const current = new Array(cols);
    current[0] = i;
    for (let j = 1; j < cols; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      current[j] = Math.min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost);
    }
    previous = current;
  }
  return previous[cols - 1];
}

const known = $input.all()
  .map((item) => item.json || {})
  .map((row) => String(row.order_id == null ? '' : row.order_id))
  .filter((id) => id);

const close = [];
for (const id of known) {
  if (distance(orderId, id) === 1) close.push(id);
}

let response;
if (close.length === 1) {
  response = `I couldn't find order #${orderId}. Did you mean order #${close[0]}?`;
} else {
  response = `I couldn't find order #${orderId}. Please check the order number.`;
}

return [{ json: { response, sessionId } }];
"""

NORMALIZE_AI_RESPONSE_CODE = r"""
// The AI Agent exposes its answer as `output`. The voice contract requires
// `response`, for both branches.
const item = $input.first();
const data = item.json || {};
const source = $('Extract Order ID').first().json;
const sessionId = String(source.sessionId || 'anonymous');
const chatInput = String(source.chatInput || '');

let response = data.output;
if (response === undefined || response === null || response === '') {
  response = data.response;
}
if (response === undefined || response === null || response === '') {
  response = data.text;
}
if (typeof response !== 'string') {
  response = response == null ? '' : JSON.stringify(response);
}

// Emoji and symbols cannot be spoken: the phonemiser warns about them and
// the text to speech engine reads them as noise.
response = response
  .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{2900}-\u{297F}\u{FE0F}\u{200D}\u{20E3}\u{FE0F}]/gu, ' ')
  .replace(/[\u2018\u2019\u201C\u201D]/g, "'")
  .replace(/\s+/g, ' ')
  .trim();

// The small model sometimes invents a personal name. A question about the
// agent's identity always gets the same, correct answer.
if (/\b(what'?s your name|what is your name|whats your name|who are you|introduce yourself)\b/i.test(chatInput)) {
  response = "I'm the DemoShop support assistant. What can I help you with?";
}

if (!response) {
  response = "I'm sorry, I couldn't generate an answer just now. Please try again.";
}

return [{ json: { response, sessionId } }];
"""

FORMAT_AI_ERROR_CODE = r"""
// Connected to the AI Agent's error output (for example, Ollama unavailable).
const source = $('Extract Order ID').first().json;
const sessionId = String(source.sessionId || 'anonymous');
return [{ json: {
  response: "I'm having trouble answering that right now. Please try again shortly.",
  sessionId
} }];
"""

SYSTEM_MESSAGE = """You are the DemoShop support assistant, the customer service agent of DemoShop, an online electronics store.

Your identity:
- Your name is "DemoShop Support". Never use a personal first name, never say you are human, never mention another brand or company.
- Never describe your feelings, your health or your plans. You are a support agent, not a person.
- Never use emoji, emoticons or decorative symbols. Write plain spoken sentences only.

How to answer:
- Keep every answer to one or two short sentences, at most about 25 words. It is spoken out loud.
- Greet the customer briefly and then ask what they need, for example: "Hi, this is DemoShop support. What can I help you with?"
- If the customer greets you or asks who you are, greet them and ask for the order number in the same breath.
- Never invent order statuses, order numbers or tracking numbers. If an order number is missing, ask for it.
- If the customer asks something unrelated to the store, answer in one short sentence and bring the conversation back to their order.
- Reply in the same language as the customer.

Store policies:
- Customers can return products within 14 days of delivery.
- Standard shipping takes 2-4 business days.
- Shipping is free for orders over 1000 TRY.
- Customer support hours are Monday to Friday, 09:00-18:00.
"""


def node(name, node_type, version, position, parameters, **extra):
    data = {
        "parameters": parameters,
        "type": node_type,
        "typeVersion": version,
        "position": position,
        "id": str(uuid.uuid4()),
        "name": name,
    }
    data.update(extra)
    return data


def build(webhook_path, table_id=TABLE_ID, webhook_id=None, credential=None):
    webhook_id = webhook_id or str(uuid.uuid4())
    credential = credential or OLLAMA_CREDENTIAL

    nodes = [
        node(
            "Voice Agent Webhook", "n8n-nodes-base.webhook", 2, [0, 0],
            {
                "httpMethod": "POST",
                "path": webhook_path,
                "responseMode": "responseNode",
                "options": {},
            },
            webhookId=webhook_id,
        ),
        node(
            "Normalize Input", "n8n-nodes-base.code", 2, [220, 0],
            {"mode": "runOnceForAllItems", "jsCode": NORMALIZE_INPUT_CODE},
        ),
        node(
            "Extract Order ID", "n8n-nodes-base.code", 2, [440, 0],
            {"mode": "runOnceForAllItems", "jsCode": EXTRACT_ORDER_ID_CODE},
        ),
        node(
            "Has Order ID?", "n8n-nodes-base.if", 2.2, [660, 0],
            {
                "conditions": {
                    "combinator": "and",
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 2,
                    },
                    "conditions": [
                        {
                            "id": "has-order-id",
                            "leftValue": "={{ $json.route }}",
                            "rightValue": "order",
                            "operator": {
                                "type": "string",
                                "operation": "equals",
                            },
                        }
                    ],
                },
                "options": {},
            },
        ),
        node(
            "Lookup Order", "n8n-nodes-base.dataTable", 1.1, [900, -160],
            {
                "resource": "row",
                "operation": "get",
                "dataTableId": {
                    "__rl": True,
                    "mode": "id",
                    "value": table_id,
                },
                "matchType": "allConditions",
                "filters": {
                    "conditions": [
                        {
                            "keyName": "order_id",
                            "condition": "eq",
                            "keyValue": "={{ $json.orderId }}",
                        }
                    ]
                },
                "returnAll": False,
                "limit": 1,
                "options": {},
            },
            alwaysOutputData=True,
            onError="continueErrorOutput",
        ),
        node(
            "Order Found?", "n8n-nodes-base.if", 2.2, [1140, -160],
            {
                "conditions": {
                    "combinator": "and",
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 2,
                    },
                    "conditions": [
                        {
                            # NOTE: the "isNotEmpty" string operator always
                            # evaluates to false in n8n 2.44.4, so the check
                            # is written as "not equal to an empty value".
                            "id": "order-found",
                            "leftValue": "={{ $json.order_id }}",
                            "rightValue": "",
                            "operator": {
                                "type": "string",
                                "operation": "notEquals",
                            },
                        }
                    ],
                },
                "options": {},
            },
        ),
        node(
            "Format Order Result", "n8n-nodes-base.code", 2, [1360, -220],
            {"mode": "runOnceForAllItems", "jsCode": FORMAT_ORDER_RESULT_CODE},
        ),
        node(
            "List Known Orders", "n8n-nodes-base.dataTable", 1.1, [1360, -60],
            {
                "resource": "row",
                "operation": "get",
                "dataTableId": {
                    "__rl": True,
                    "mode": "id",
                    "value": table_id,
                },
                "matchType": "allConditions",
                "filters": {"conditions": []},
                "returnAll": True,
                "options": {},
            },
            alwaysOutputData=True,
        ),
        node(
            "Suggest Similar Order", "n8n-nodes-base.code", 2, [1580, -60],
            {"mode": "runOnceForAllItems", "jsCode": FORMAT_SUGGEST_ORDER_CODE},
        ),
        node(
            "Format Lookup Error", "n8n-nodes-base.code", 2, [1140, -320],
            {"mode": "runOnceForAllItems", "jsCode": FORMAT_LOOKUP_ERROR_CODE},
        ),
        node(
            "Needs Clarification?", "n8n-nodes-base.if", 2.2, [900, 160],
            {
                "conditions": {
                    "combinator": "and",
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 2,
                    },
                    "conditions": [
                        {
                            "id": "needs-clarification",
                            "leftValue": "={{ $json.route }}",
                            "rightValue": "general",
                            "operator": {
                                "type": "string",
                                "operation": "notEquals",
                            },
                        }
                    ],
                },
                "options": {},
            },
        ),
        node(
            "Format Clarification", "n8n-nodes-base.code", 2, [1140, 320],
            {"mode": "runOnceForAllItems", "jsCode": FORMAT_CLARIFICATION_CODE},
        ),
        node(
            "AI Agent", "@n8n/n8n-nodes-langchain.agent", 3.1, [1140, 40],
            {
                "promptType": "define",
                "text": "={{ $json.chatInput }}",
                "options": {"systemMessage": SYSTEM_MESSAGE},
            },
            onError="continueErrorOutput",
        ),
        node(
            "Normalize AI Response", "n8n-nodes-base.code", 2, [1380, 40],
            {"mode": "runOnceForAllItems", "jsCode": NORMALIZE_AI_RESPONSE_CODE},
        ),
        node(
            "Format AI Error", "n8n-nodes-base.code", 2, [1380, -120],
            {"mode": "runOnceForAllItems", "jsCode": FORMAT_AI_ERROR_CODE},
        ),
        node(
            "Ollama Chat Model", "@n8n/n8n-nodes-langchain.lmChatOllama", 1,
            [1100, 260],
            {"model": "qwen3:4b-instruct", "options": {}},
            credentials=credential,
        ),
        node(
            "Simple Memory", "@n8n/n8n-nodes-langchain.memoryBufferWindow", 1.4,
            [1220, 260],
            {
                "sessionIdType": "customKey",
                "sessionKey": "={{ $json.sessionId }}",
                "contextWindowLength": 10,
            },
        ),
        node(
            "Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.1,
            [1800, 0],
            {
                "respondWith": "json",
                "responseBody": "={{ { response: $json.response, sessionId: $json.sessionId } }}",
                "options": {},
            },
        ),
    ]

    def main(*targets):
        return [[{"node": t, "type": "main", "index": 0} for t in targets]]

    connections = {
        "Voice Agent Webhook": {"main": main("Normalize Input")},
        "Normalize Input": {"main": main("Extract Order ID")},
        "Extract Order ID": {"main": main("Has Order ID?")},
        "Has Order ID?": {
            "main": [
                [{"node": "Lookup Order", "type": "main", "index": 0}],
                [{"node": "Needs Clarification?", "type": "main", "index": 0}],
            ]
        },
        "Lookup Order": {
            "main": [
                [{"node": "Order Found?", "type": "main", "index": 0}],
                [{"node": "Format Lookup Error", "type": "main", "index": 0}],
            ]
        },
        "Order Found?": {
            "main": [
                [{"node": "Format Order Result", "type": "main", "index": 0}],
                [{"node": "List Known Orders", "type": "main", "index": 0}],
            ]
        },
        "Format Order Result": {"main": main("Respond to Webhook")},
        "List Known Orders": {"main": main("Suggest Similar Order")},
        "Suggest Similar Order": {"main": main("Respond to Webhook")},
        "Format Lookup Error": {"main": main("Respond to Webhook")},
        "Needs Clarification?": {
            "main": [
                [{"node": "Format Clarification", "type": "main", "index": 0}],
                [{"node": "AI Agent", "type": "main", "index": 0}],
            ]
        },
        "Format Clarification": {"main": main("Respond to Webhook")},
        "AI Agent": {
            "main": [
                [{"node": "Normalize AI Response", "type": "main", "index": 0}],
                [{"node": "Format AI Error", "type": "main", "index": 0}],
            ]
        },
        "Normalize AI Response": {"main": main("Respond to Webhook")},
        "Format AI Error": {"main": main("Respond to Webhook")},
        "Ollama Chat Model": {
            "ai_languageModel": [[{"node": "AI Agent", "type": "ai_languageModel", "index": 0}]]
        },
        "Simple Memory": {
            "ai_memory": [[{"node": "AI Agent", "type": "ai_memory", "index": 0}]]
        },
    }

    return {
        "name": "DemoShop Voice Agent API",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "binaryMode": "separate"},
    }


def build_parser_test_workflow(webhook_path: str, webhook_id=None) -> dict:
    """
    Small workflow that runs only the parser and returns its decision.

    It embeds the same parser source as the production workflow, so the
    integration tests exercise the code that really runs in n8n.
    """
    webhook_id = webhook_id or str(uuid.uuid4())

    nodes = [
        node(
            "Voice Agent Webhook", "n8n-nodes-base.webhook", 2, [0, 0],
            {
                "httpMethod": "POST",
                "path": webhook_path,
                "responseMode": "responseNode",
                "options": {},
            },
            webhookId=webhook_id,
        ),
        node(
            "Normalize Input", "n8n-nodes-base.code", 2, [220, 0],
            {"mode": "runOnceForAllItems", "jsCode": NORMALIZE_INPUT_CODE},
        ),
        node(
            "Extract Order ID", "n8n-nodes-base.code", 2, [440, 0],
            {"mode": "runOnceForAllItems", "jsCode": EXTRACT_ORDER_ID_CODE},
        ),
        node(
            "Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.1, [660, 0],
            {
                "respondWith": "json",
                "responseBody": (
                    "={{ { route: $json.route, orderId: $json.orderId, "
                    "confidence: $json.confidence, candidates: $json.candidates, "
                    "clarification: $json.clarification } }}"
                ),
                "options": {},
            },
        ),
    ]

    def main(*targets):
        return [[{"node": t, "type": "main", "index": 0} for t in targets]]

    connections = {
        "Voice Agent Webhook": {"main": main("Normalize Input")},
        "Normalize Input": {"main": main("Extract Order ID")},
        "Extract Order ID": {"main": main("Respond to Webhook")},
    }

    return {
        "name": "DemoShop Order Parser TEST",
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1", "binaryMode": "separate"},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--webhook-path", default="voice-agent")
    parser.add_argument("--table-id", default=TABLE_ID)
    parser.add_argument(
        "--ollama-credential-id", default=OLLAMA_CREDENTIAL["ollamaApi"]["id"],
        help="n8n credential id of the Ollama account",
    )
    parser.add_argument("--webhook-id", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument(
        "--parser-test", action="store_true",
        help="build the small parser-only test workflow",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if args.parser_test:
        workflow = build_parser_test_workflow(args.webhook_path, args.webhook_id)
    else:
        credential = {
            "ollamaApi": {
                "id": args.ollama_credential_id,
                "name": OLLAMA_CREDENTIAL["ollamaApi"]["name"],
            }
        }
        workflow = build(
            args.webhook_path, args.table_id, args.webhook_id, credential
        )
    if args.name:
        workflow["name"] = args.name
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(workflow, fh, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out} ({len(workflow['nodes'])} nodes, path={args.webhook_path})")


if __name__ == "__main__":
    main()

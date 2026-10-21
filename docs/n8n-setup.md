# Setting up n8n

## 1. Requirements

* n8n (tested on 2.44.4) running locally or in Docker.
* Ollama with the chat model the workflow expects:

  ```bash
  ollama pull qwen3:4b-instruct
  ```

  From a Docker n8n container the host is reachable at
  `http://host.docker.internal:11434`.

## 2. The Data Table

Create a Data Table named `customer_orders` with these columns (all
`string`):

| Column | Example |
|---|---|
| `order_id` | `1001` |
| `customer_name` | `Ahmet` |
| `status` | `Shipped` |
| `product` | `iPhone 15` |
| `tracking_number` | `TR123456` |

Order ids are stored as strings. The workflow filters on `order_id` with an
`eq` condition, so the value it sends has to match the stored value exactly.

## 3. The Ollama credential

The AI Agent needs an **Ollama Chat Model** node with a credential of type
*Ollama account* whose base URL points at your Ollama instance. The
credential is referenced by id in the workflow JSON, so an imported workflow
has to have its own credential selected once.

## 4. Deploy the workflow

The workflow is generated, not edited by hand:

```bash
export N8N_API_KEY=n8n_api_...
export N8N_BASE_URL=http://localhost:5678
export N8N_WORKFLOW_ID=<your workflow id>   # an existing workflow
./scripts/deploy_workflow.sh
```

or create a second one:

```bash
./scripts/deploy_workflow.sh --new
```

The generator writes `workflows/voice_agent_production.json`, which is also
the file to import by hand through the n8n UI.

The identifiers of your instance can be set as well, they are only needed
when the workflow is built for a different n8n:

| Variable | Meaning |
|---|---|
| `N8N_DATA_TABLE_ID` | id of the `customer_orders` Data Table |
| `N8N_OLLAMA_CREDENTIAL_ID` | id of the Ollama credential of the AI Agent |
| `N8N_WEBHOOK_PATH` | path of the webhook, `voice-agent` by default |

The deploy script keeps the webhook id of the workflow that is already
deployed, makes sure the workflow ends up active and verifies that the
webhook answers afterwards.

## 5. The webhook

The workflow answers on `POST /webhook/voice-agent` with:

```json
{ "response": "Order 1001 has shipped. It's the iPhone 15, tracking number TR123456.",
  "sessionId": "abc123" }
```

`response` is always a non empty string. `sessionId` is echoed back so the
client can keep one conversation per session.

## 6. Check it

```bash
curl -s -X POST http://localhost:5678/webhook/voice-agent \
  -H 'Content-Type: application/json' \
  -d '{"chatInput":"Where is my order 1001?","sessionId":"manual"}'
```

## Notes for editing the workflow in the UI

* **Never use the "is not empty" condition on a string.** In n8n 2.44.4 that
  operator always evaluates to false, which silently routes every item to
  the false branch. Use "not equal to" with an empty value instead.
* A Code node runs in `runOnceForAllItems` mode has to return
  `[{ json: { ... } }]`, not a bare object.
* The Code node has no `$httpRequest`; query the Data Table with the native
  Data Table node instead.

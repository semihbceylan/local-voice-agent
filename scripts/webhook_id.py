#!/usr/bin/env python3
"""
Print the webhook id of a workflow JSON (empty when there is none).

Usage:  python3 scripts/webhook_id.py workflows/voice_agent_production.json
"""

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: webhook_id.py <workflow.json>", file=sys.stderr)
        return 1
    with open(sys.argv[1], encoding="utf-8") as handle:
        workflow = json.load(handle)
    for node in workflow.get("nodes", []):
        if node.get("name") == "Voice Agent Webhook" and node.get("webhookId"):
            print(node["webhookId"])
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

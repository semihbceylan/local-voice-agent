#!/usr/bin/env python3
"""
End-to-end integration tests for the voice agent backend.

Usage:
    # create isolated workflows, test them, then deactivate them again
    .venv/bin/python tests/test_integration.py --deploy

    # test an already running pair of workflows
    .venv/bin/python tests/test_integration.py \
        --voice-url http://localhost:5678/webhook/voice-agent-test \
        --parser-url http://localhost:5678/webhook/parser-test

The tests only use the public webhook contract. When the n8n API key is
available the execution traces are inspected as well, to prove that order
lookups use the Data Table node and never the LLM.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:5678/api/v1"

PARSER_CASES = [
    # text, expected route, expected order id, expected confidence
    ("Where is my order 1001?", "order", "1001", "exact"),
    ("Check order 1,001", "order", "1001", "exact"),
    ("Check order one thousand and one", "order", "1001", "exact"),
    ("Order number one thousand and two", "order", "1002", "exact"),
    ("My order number is 1,000 and 1.", "order", "1001", "recovered"),
    ("Yes, my order number is 1000 and 1.", "order", "1001", "recovered"),
    ("My order number is 1,000 and 1. Find it.", "order", "1001", "recovered"),
    (
        "Hi them shop, how are you today? I want to take information about my order 1000 and a 1.",
        "order", "1001", "recovered",
    ),
    ("1,001", "order", "1001", "exact"),
    ("Hi DemoShop, where is my older 1000 at 1?", "uncertain", "", "uncertain"),
    ("What about order 1001 and order 1002?", "ambiguous", "", "ambiguous"),
    ("Check order 1000 and 1001", "ambiguous", "", "ambiguous"),
    ("How do I return a product?", "general", "", "none"),
    ("My order is from 2026 and I waited 14 days", "general", "", "none"),
    ("My phone number is 555 123 4567", "general", "", "none"),
    ("", "empty", "", "none"),
]

ORDER_CASES = [
    ("Where is my order 1001?", ["Order 1001", "shipped", "iPhone 15", "TR123456"]),
    ("Check order 1,001", ["1001", "shipped", "TR123456"]),
    ("Check order one thousand and one", ["1001", "shipped", "TR123456"]),
    ("My order number is 1,000 and 1.", ["1001", "shipped", "TR123456"]),
    (
        "Hi them shop, how are you today? I want to take information about my order 1000 and a 1.",
        ["1001", "shipped", "TR123456"],
    ),
    ("Where is my order 1002?", ["1002", "processed", "MacBook Air M3", "No tracking number"]),
    ("Check order 1003", ["1003", "delivered", "AirPods Pro", "TR789012"]),
    ("What about order 9999?", ["couldn't find order #9999"]),
    ("Where is my order 1011?", ["couldn't find order #1011", "Did you mean order #1001"]),
    ("Check order 1009", ["couldn't find order #1009"]),
    ("Where is my older 1000 at 1?", ["repeat the order number"]),
    ("What about order 1001 and order 1002?", ["more than one possible order number"]),
    ("", ["didn't catch a question"]),
]

GENERAL_CASES = [
    ("How do I return a product?", ["14 days"]),
]


class Result:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, name, condition, detail=""):
        if condition:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name} :: {detail}")


def post(url, payload, timeout=120):
    start = time.perf_counter()
    response = requests.post(url, json=payload, timeout=timeout)
    return response, time.perf_counter() - start


def deploy_isolated(result: Result):
    """Create, activate and return isolated voice + parser workflows."""
    key = os.environ.get("N8N_API_KEY")
    if not key:
        raise SystemExit("N8N_API_KEY is required to deploy the isolated workflows")
    headers = {"X-N8N-API-KEY": key, "Content-Type": "application/json"}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    voice_path = f"voice-agent-itest-{stamp}"
    parser_path = f"parser-itest-{stamp}"

    created = []
    for path, extra in ((voice_path, []), (parser_path, ["--parser-test"])):
        out = f"/tmp/itest_{path}.json"
        subprocess.run(
            [sys.executable, str(ROOT / "build_voice_workflow.py"),
             "--webhook-path", path, "--out", out] + extra,
            check=True, cwd=str(ROOT),
        )
        workflow = json.load(open(out))
        response = requests.post(f"{BASE}/workflows", headers=headers, json=workflow, timeout=30)
        response.raise_for_status()
        created.append(response.json()["id"])
        requests.post(f"{BASE}/workflows/{response.json()['id']}/activate", headers=headers, timeout=30)
    time.sleep(2)
    return (
        f"http://localhost:5678/webhook/{voice_path}",
        f"http://localhost:5678/webhook/{parser_path}",
        created,
    )


def run_tests(voice_url, parser_url, result: Result):
    if parser_url:
        print("\n== parser decisions (isolated parser workflow) ==")
        for text, route, order_id, confidence in PARSER_CASES:
            response, _ = post(parser_url, {"chatInput": text, "sessionId": "itest"})
            body = response.json()
            ok = (
                body.get("route") == route
                and body.get("orderId") == order_id
                and body.get("confidence") == confidence
            )
            result.check(
                f"parser {text!r} -> {route}/{order_id or '-'}/{confidence}",
                ok,
                f"got {body}",
            )
    else:
        print("\n== parser decisions skipped (no parser url) ==")

    print("\n== voice webhook contract ==")
    for text, expected in ORDER_CASES:
        response, elapsed = post(voice_url, {"chatInput": text, "sessionId": "itest"})
        body = response.json()
        answer = body.get("response", "")
        ok = response.status_code == 200 and isinstance(answer, str) and answer.strip()
        ok = ok and all(part.lower() in answer.lower() for part in expected)
        if text == "What about order 9999?":
            # A far away number must not produce a "did you mean" suggestion.
            ok = ok and "did you mean" not in answer.lower()
        if text == "Check order 1009":
            # Three orders are one digit away, so no suggestion is possible.
            ok = ok and "did you mean" not in answer.lower()
        result.check(
            f"voice {text!r} ({elapsed:.2f}s)",
            ok,
            f"got {body}",
        )

    print("\n== general conversation ==")
    for text, expected in GENERAL_CASES:
        response, elapsed = post(voice_url, {"chatInput": text, "sessionId": "itest"})
        answer = response.json().get("response", "")
        ok = all(part.lower() in answer.lower() for part in expected)
        result.check(f"general {text!r} ({elapsed:.2f}s)", ok, f"got {answer!r}")

    print("\n== session memory ==")
    post(voice_url, {"chatInput": "My name is Alex and I live in Lisbon.", "sessionId": "mem-a"})
    response, _ = post(voice_url, {"chatInput": "What is my name and where do I live?", "sessionId": "mem-a"})
    answer = response.json().get("response", "")
    # The model may hyphenate the name, so compare on letters only.
    result.check(
        "memory remembers within a session",
        "alex" in "".join(ch for ch in answer.lower() if ch.isalpha()),
        f"got {answer!r}",
    )
    response, _ = post(voice_url, {"chatInput": "What is my name and where do I live?", "sessionId": "mem-b"})
    answer = response.json().get("response", "")
    result.check(
        "memory is isolated between sessions",
        "alex" not in "".join(ch for ch in answer.lower() if ch.isalpha()),
        f"got {answer!r}",
    )


def check_traces(workflow_id, result: Result):
    """Prove that order lookups use the Data Table and never the LLM."""
    key = os.environ.get("N8N_API_KEY")
    if not key or not workflow_id:
        print("\n== execution traces skipped (no API key or workflow id) ==")
        return
    headers = {"X-N8N-API-KEY": key}
    post(voice_url_global, {"chatInput": "Where is my order 1003?", "sessionId": "trace"})
    time.sleep(0.5)
    executions = requests.get(
        f"{BASE}/executions?limit=5&workflowId={workflow_id}", headers=headers, timeout=30
    ).json().get("data", [])
    if not executions:
        result.check("trace for an order lookup", False, "no executions found")
        return
    execution = requests.get(
        f"{BASE}/executions/{executions[0]['id']}?includeData=true", headers=headers, timeout=30
    ).json()
    nodes = list(execution["data"]["resultData"]["runData"].keys())
    result.check("order lookup runs the Data Table node", "Lookup Order" in nodes, f"nodes={nodes}")
    result.check("order lookup does not run the LLM", "AI Agent" not in nodes, f"nodes={nodes}")

    post(voice_url_global, {"chatInput": "How do I return a product?", "sessionId": "trace"})
    time.sleep(0.5)
    executions = requests.get(
        f"{BASE}/executions?limit=5&workflowId={workflow_id}", headers=headers, timeout=30
    ).json().get("data", [])
    execution = requests.get(
        f"{BASE}/executions/{executions[0]['id']}?includeData=true", headers=headers, timeout=30
    ).json()
    nodes = list(execution["data"]["resultData"]["runData"].keys())
    result.check("general question runs the AI Agent", "AI Agent" in nodes, f"nodes={nodes}")
    result.check("general question does not touch the Data Table", "Lookup Order" not in nodes, f"nodes={nodes}")


def check_simulated_failure(voice_url, result: Result):
    """Point the isolated workflow at a missing table and check the error path."""
    key = os.environ.get("N8N_API_KEY")
    if not key:
        print("\n== simulated lookup failure skipped (no API key) ==")
        return
    headers = {"X-N8N-API-KEY": key, "Content-Type": "application/json"}
    workflow_id = os.environ.get("ITEST_WORKFLOW_ID")
    if not workflow_id:
        print("\n== simulated lookup failure skipped (no workflow id) ==")
        return

    current = requests.get(f"{BASE}/workflows/{workflow_id}", headers=headers, timeout=30).json()
    webhook = [n for n in current["nodes"] if n["name"] == "Voice Agent Webhook"][0]
    out = "/tmp/itest_broken.json"
    subprocess.run(
        [sys.executable, str(ROOT / "build_voice_workflow.py"),
         "--webhook-path", webhook["parameters"]["path"],
         "--webhook-id", webhook.get("webhookId", ""),
         "--out", out],
        check=True, cwd=str(ROOT),
    )
    good = json.load(open(out))
    broken = json.loads(json.dumps(good))
    for n in broken["nodes"]:
        if n["name"] == "Lookup Order":
            n["parameters"]["dataTableId"]["value"] = "DOES_NOT_EXIST_TEST"

    response = requests.put(f"{BASE}/workflows/{workflow_id}", headers=headers, json=broken, timeout=30)
    result.check("isolated workflow accepts the broken table", response.status_code == 200, response.text[:200])
    time.sleep(2)
    try:
        response, _ = post(voice_url, {"chatInput": "Where is my order 1001?", "sessionId": "err"})
        answer = response.json().get("response", "")
        ok = "trouble accessing order information" in answer.lower() and "couldn't find" not in answer.lower()
        result.check("simulated database failure returns a temporary error", ok, f"got {answer!r}")
    finally:
        response = requests.put(f"{BASE}/workflows/{workflow_id}", headers=headers, json=good, timeout=30)
        result.check("isolated workflow restored", response.status_code == 200, response.text[:200])
        time.sleep(2)
        response, _ = post(voice_url, {"chatInput": "Where is my order 1001?", "sessionId": "err"})
        answer = response.json().get("response", "")
        result.check("lookup works again after restoring the table", "TR123456" in answer, f"got {answer!r}")


voice_url_global = None


def main():
    global voice_url_global
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice-url", default=None)
    parser.add_argument("--parser-url", default=None)
    parser.add_argument("--deploy", action="store_true", help="create isolated workflows first")
    parser.add_argument("--keep", action="store_true", help="keep the deployed workflows active")
    args = parser.parse_args()

    result = Result()
    created = []
    if args.deploy:
        voice_url, parser_url, created = deploy_isolated(result)
        print(f"deployed isolated voice workflow : {voice_url}")
        print(f"deployed isolated parser workflow: {parser_url}")
    else:
        if not args.voice_url:
            raise SystemExit("--voice-url is required without --deploy")
        voice_url = args.voice_url
        parser_url = args.parser_url
    voice_url_global = voice_url

    if created:
        os.environ["ITEST_WORKFLOW_ID"] = created[0]

    try:
        run_tests(voice_url, parser_url, result)
        check_traces(created[0] if created else os.environ.get("ITEST_WORKFLOW_ID"), result)
        if created:
            check_simulated_failure(voice_url, result)
    finally:
        if created and not args.keep:
            headers = {"X-N8N-API-KEY": os.environ["N8N_API_KEY"]}
            for workflow_id in created:
                requests.post(f"{BASE}/workflows/{workflow_id}/deactivate", headers=headers, timeout=30)
            print(f"\ndeactivated isolated workflows: {', '.join(created)}")

    print(f"\n{result.passed} passed, {len(result.failed)} failed")
    for name, detail in result.failed:
        print(f"  FAILED: {name} :: {detail}")
    sys.exit(1 if result.failed else 0)


if __name__ == "__main__":
    main()

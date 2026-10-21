#!/usr/bin/env python3
"""
Guard: the committed workflow must match what the builder produces.

The builder generates a fresh node id and a fresh webhook id on every run,
so those two fields are ignored. Everything else (nodes, parameters,
connections, the embedded parser) has to be identical, otherwise the file in
workflows/ has drifted away from build_voice_workflow.py.

Run:  .venv/bin/python tests/test_workflow_build.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "workflows" / "voice_agent_production.json"


def normalise(workflow: dict) -> dict:
    """Drop the fields that are generated randomly on every build."""
    copy = json.loads(json.dumps(workflow))
    for node in copy.get("nodes", []):
        node.pop("id", None)
        node.pop("webhookId", None)
    return copy


class WorkflowBuildTests(unittest.TestCase):
    def test_committed_workflow_matches_the_builder(self):
        if not COMMITTED.exists():
            self.skipTest("no committed workflow")
        committed = json.loads(COMMITTED.read_text())
        path = next(
            n["parameters"]["path"]
            for n in committed["nodes"]
            if n["name"] == "Voice Agent Webhook"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "workflow.json"
            subprocess.run(
                [sys.executable, str(ROOT / "build_voice_workflow.py"),
                 "--webhook-path", path, "--out", str(out)],
                check=True, cwd=str(ROOT),
            )
            built = json.loads(out.read_text())
        self.assertEqual(
            normalise(built), normalise(committed),
            "workflows/voice_agent_production.json is out of date, "
            "rebuild it with: python3 build_voice_workflow.py "
            "--webhook-path voice-agent --out workflows/voice_agent_production.json",
        )

    def test_the_workflow_carries_no_secret(self):
        if not COMMITTED.exists():
            self.skipTest("no committed workflow")
        text = COMMITTED.read_text()
        self.assertNotIn("eyJhbGci", text, "an API key is embedded in the workflow")
        self.assertNotIn("n8n_api_", text, "an API key is embedded in the workflow")

    def test_the_parser_in_the_workflow_is_the_source_file(self):
        if not COMMITTED.exists():
            self.skipTest("no committed workflow")
        committed = json.loads(COMMITTED.read_text())
        source = (ROOT / "workflow" / "extract_order_id.js").read_text()
        node = next(n for n in committed["nodes"] if n["name"] == "Extract Order ID")
        self.assertEqual(
            node["parameters"]["jsCode"], source,
            "the parser in the workflow is not the file in workflow/",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

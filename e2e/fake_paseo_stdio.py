"""A stdio MCP server that looks like paseo-mcp's write tools to Hermes and records every call.

Tools: paseo_create_agent (returns a fixed agent id) and paseo_send_message. Each call's arguments
(including the labels the bridge's pre_tool_call hook added) are appended to $FAKE_PASEO_LOG.
Newline-delimited JSON-RPC 2.0, as the MCP stdio transport specifies. Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
import time

LOG = os.environ.get("FAKE_PASEO_LOG", "/tmp/fake_paseo.jsonl")
AGENT_ID = os.environ.get("FAKE_PASEO_AGENT_ID", "agent-s5-0001")

TOOLS = [
    {"name": "paseo_create_agent", "description": "Start a new Paseo agent",
     "inputSchema": {"type": "object", "properties": {
         "cwd": {"type": "string"}, "prompt": {"type": "string"}, "title": {"type": "string"},
         "labels": {"type": "object", "additionalProperties": {"type": "string"}}},
         "required": ["cwd", "prompt"]}},
    {"name": "paseo_send_message", "description": "Send a message to an agent",
     "inputSchema": {"type": "object", "properties": {
         "agent_id": {"type": "string"}, "text": {"type": "string"},
         "when_running": {"type": "string", "enum": ["steer", "interrupt"]}},
         "required": ["agent_id", "text"]}},
]


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue  # notifications
    if method == "initialize":
        reply(rid, {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-paseo-mcp", "version": "0.0.0"}})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        p = msg["params"]
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"at": time.time(), "tool": p["name"], "arguments": p.get("arguments"),
                                 "meta": p.get("_meta")}) + "\n")
        if p["name"] == "paseo_create_agent":
            out = {"id": AGENT_ID, "cwd": p["arguments"].get("cwd"), "hint": "Running."}
        else:
            out = {"id": p["arguments"].get("agent_id"), "sent": True}
        reply(rid, {"content": [{"type": "text", "text": json.dumps(out)}]})
    elif method == "ping":
        reply(rid, {})
    else:
        reply(rid, error={"code": -32601, "message": f"unknown method {method}"})

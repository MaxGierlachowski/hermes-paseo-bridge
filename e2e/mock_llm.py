"""A scripted OpenAI-compatible chat-completions endpoint, so Hermes runs real turns without an LLM.

- A user prompt containing "start a paseo agent" -> one tool call to the exposed ``*paseo_create_agent``
  tool (whatever prefix Hermes gives MCP tools), then a short final answer after the tool result.
- A user prompt containing "steer agent <id>" -> a tool call to ``*paseo_send_message``.
- A turn whose newest user message is a bridge notice ("[Paseo agent update]") -> "ACK <first line>".
- Anything else (titles, summaries) -> a short text.
Every request is appended to $MOCK_LLM_LOG (JSON lines) as evidence. Streams when asked to.
"""
from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LLM_LOG", "/tmp/mock_llm.jsonl")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def decide(body: dict) -> dict:
    msgs = body.get("messages") or []
    tools = [t.get("function", {}).get("name", "") for t in body.get("tools") or []]
    last = msgs[-1] if msgs else {}
    users = [m for m in msgs if m.get("role") == "user"]
    newest_user = _text(users[-1].get("content")) if users else ""

    def call(suffix: str, args: dict):
        """Call the MCP tool directly when exposed, else through Hermes' deferred ``tool_call`` bridge."""
        name = next((t for t in tools if t.endswith(suffix)), None)
        if name:
            return {"tool": name, "args": args}
        if "tool_call" in tools:
            return {"tool": "tool_call", "args": {"calls": [{"name": f"mcp__paseo__{suffix}", "arguments": args}]}}
        return {"text": f"NO_TOOL {suffix} in " + ",".join(tools)}

    if last.get("role") == "tool":
        return {"text": "Done: " + _text(last.get("content"))[:120]}
    if "[Paseo agent update]" in newest_user:
        first = next((ln for ln in newest_user.splitlines() if ln.startswith("- ")), "")
        return {"text": "ACK " + first[:200]}
    if "start a paseo agent" in newest_user.lower():
        return call("paseo_create_agent", {"cwd": "/home/agentbox/work/demo", "prompt": "Fix the failing test",
                                           "title": "s5 demo", "labels": {"todo": "T-1"}})
    if "steer agent " in newest_user.lower():
        agent = newest_user.lower().split("steer agent ", 1)[1].split()[0]
        return call("paseo_send_message", {"agent_id": agent, "text": "Also update the docs."})
    return {"text": "ok"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "mock-model", "object": "model", "owned_by": "s5"}]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": f"unsupported path {self.path}"}})
        d = decide(body)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"at": time.time(), "decision": d, "messages": body.get("messages"),
                                 "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
                                 "stream": bool(body.get("stream"))}) + "\n")
        model = body.get("model") or "mock-model"
        cid = f"chatcmpl-{int(time.time() * 1000)}"
        if d.get("tool"):
            call = {"id": f"call_{int(time.time() * 1000)}", "type": "function",
                    "function": {"name": d["tool"], "arguments": json.dumps(d["args"])}}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": d["text"]}, "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            return self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                                    "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish_reason=None, **extra):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra}
            self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
            self.wfile.flush()

        if d.get("tool"):
            c = message["tool_calls"][0]
            chunk({"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": c["id"], "type": "function", "function": {"name": c["function"]["name"], "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": c["function"]["arguments"]}}]})
        else:
            chunk({"role": "assistant", "content": message["content"]})
        chunk({}, finish)
        self.wfile.write(b"data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                                                 "model": model, "choices": [], "usage": usage}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"mock LLM on http://127.0.0.1:{port}/v1", flush=True)
    srv.serve_forever()

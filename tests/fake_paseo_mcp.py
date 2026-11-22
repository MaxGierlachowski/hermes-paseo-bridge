"""A fake paseo-mcp plugin endpoint: Streamable HTTP at /mcp with ``paseo_wait_for_events``.

Speaks the parts the bridge uses: bearer token, ``initialize`` -> ``Mcp-Session-Id``, notifications ->
202, ``tools/call`` answered as JSON or as an SSE stream, 404 for an unknown session. Events and the
cursor follow paseo-mcp (``<epoch>:<seq>``; another epoch is a gap)."""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakePaseoMcp:
    def __init__(self, token: str = "read-token-123", *, sse: bool = False, port: int = 0):
        self.token = token
        self.sse = sse
        self.epoch = "ep1"
        self.events: list[dict] = []
        self.sessions: set[str] = set()
        self.calls: list[dict] = []
        self.cond = threading.Condition()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload=None, headers=None):
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path == "/_emit":  # test control (e2e): add one event
                    fake.emit(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
                    return self._send(200, {"ok": True, "events": len(fake.events)})
                if self.path != "/mcp":
                    return self._send(404, {"error": "not found"})
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send(401, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "bad token"}, "id": None})
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                sid = self.headers.get("Mcp-Session-Id")
                if msg.get("method") == "initialize":
                    sid = str(uuid.uuid4())
                    fake.sessions.add(sid)
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake-paseo-mcp", "version": "0"}}
                    return self._answer(msg["id"], result, {"Mcp-Session-Id": sid})
                if sid not in fake.sessions:
                    return self._send(404, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Session not found"}, "id": None})
                if "id" not in msg:
                    return self._send(202)
                if msg.get("method") == "tools/call":
                    fake.calls.append(msg["params"])
                    return self._answer(msg["id"], fake.wait(msg["params"]["arguments"]))
                return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}})

            def _answer(self, rid, result, headers=None):
                body = {"jsonrpc": "2.0", "id": rid, "result": result}
                if not fake.sse:
                    return self._send(200, body, headers)
                data = (": ping\n\n" + "event: message\ndata: " + json.dumps(body) + "\n\n").encode()
                self.send_response(200)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def emit(self, event: dict) -> None:
        with self.cond:
            seq = len(self.events) + 1
            self.events.append({**event, "id": f"{self.epoch}:{seq}", "epoch": self.epoch, "seq": seq})
            self.cond.notify_all()

    def restart(self) -> None:
        """paseo-mcp restarted: a new epoch, old events gone, every session forgotten."""
        with self.cond:
            self.epoch = "ep2"
            self.events = []
            self.sessions.clear()

    def wait(self, args: dict) -> dict:
        cursor = args.get("cursor")
        with self.cond:
            if not cursor:
                return self._ok({"events": [], "next_cursor": f"{self.epoch}:{len(self.events)}", "gap": False, "more": False})
            epoch, _, seq = cursor.partition(":")
            if epoch != self.epoch:
                return self._ok({"events": [], "next_cursor": f"{self.epoch}:{len(self.events)}", "gap": True,
                                 "more": False, "agents": [{"agentId": "agent-x", "requiresAttention": True}]})
            start = int(seq)
            if len(self.events) <= start and args.get("timeout_sec"):
                self.cond.wait(timeout=min(args["timeout_sec"], 2))
            out = self.events[start:start + args.get("limit", 50)]
            types = args.get("types")
            nxt = start + len(out)
            out = [e for e in out if not types or e["type"] in types]
            return self._ok({"events": out, "next_cursor": f"{self.epoch}:{nxt}", "gap": False, "more": False})

    @staticmethod
    def _ok(data: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


if __name__ == "__main__":  # e2e: python fake_paseo_mcp.py <port>; token from FAKE_PASEO_MCP_TOKEN
    import os
    import sys

    fake = FakePaseoMcp(os.environ["FAKE_PASEO_MCP_TOKEN"], port=int(sys.argv[1]), sse="--sse" in sys.argv)
    print(f"fake paseo-mcp on {fake.url}", flush=True)
    fake.thread.join()

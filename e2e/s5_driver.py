"""S5 driver: talks to a running ``hermes gateway run`` the way hermes-ui does, and plays paseo-mcp.

Gateway access (as hermes-ui's src/hermes-ws.mjs): ``$HERMES_HOME/gateway.sock`` -> ``identify`` +
``session-ticket`` -> WebSocket ``/api/ws`` JSON-RPC (``session.create``, ``session.resume``,
``prompt.submit``). paseo-mcp is played by signing a webhook delivery with the bridge's own signature
code and posting it to the bridge's loopback receiver.

Run with the Python of the Hermes environment (it has ``websockets``):

    python s5_driver.py <step> [...]

Steps print one JSON line each; the shell script (run-s5.sh) strings them together.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import urllib.request
import uuid

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
PLUGIN_DIR = os.path.join(HERMES_HOME, "plugins", "hermes-paseo-bridge")
S5 = os.path.dirname(os.path.abspath(__file__))
MOCK_LOG = os.environ.get("MOCK_LLM_LOG", os.path.join(S5, "mock_llm.jsonl"))
PASEO_LOG = os.environ.get("FAKE_PASEO_LOG", os.path.join(S5, "fake_paseo.jsonl"))
STATE = os.path.join(S5, "state.json")


def out(**kw) -> None:
    print(json.dumps(kw, default=str), flush=True)


def load_state() -> dict:
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(**kw) -> None:
    s = load_state()
    s.update(kw)
    json.dump(s, open(STATE, "w"))


# ---------------------------------------------------------------- gateway control socket + WS

def control(verb: str, params: dict | None = None) -> dict:
    path = os.path.join(HERMES_HOME, "gateway.sock")
    pointer = path + ".path"
    if os.path.exists(pointer) and not os.path.exists(path):
        path = open(pointer).read().strip()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(path)
    req = {"protocol": 1, "id": 1, "verb": verb, **({"params": params} if params else {})}
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    msg = json.loads(buf.split(b"\n", 1)[0])
    if not msg.get("ok", True) or msg.get("error"):
        raise RuntimeError(f"control {verb}: {msg.get('error')}")
    return msg.get("result", msg)


class Gateway:
    def __init__(self):
        self.ws = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict] = []
        self.reader = None

    async def connect(self, attempts: int = 30):
        """Connect like hermes-ui; retried while the gateway finishes starting (it answers 403 until then)."""
        for i in range(attempts):
            try:
                return await self._connect()
            except Exception as exc:
                if i == attempts - 1:
                    raise
                last = exc
                await asyncio.sleep(1)
        raise RuntimeError(f"gateway WebSocket not reachable: {last}")

    async def _connect(self):
        import websockets
        ident = control("identify")
        profiles = ident.get("served_profiles") or []
        profile = next((p for p in profiles if p.get("profile_id") == HERMES_HOME), profiles[0] if profiles else {})
        ticket = control("session-ticket", {"profile_id": profile.get("profile_id", HERMES_HOME),
                                            "instance_id": ident["instance_id"], "purpose": "interactive",
                                            "scope": "host"})["ticket"]
        url = ident["api_origin"].replace("http", "ws", 1) + "/api/ws"
        self.ws = await websockets.connect(url, subprotocols=["hermes-gateway-v1", f"hermes-gateway-ticket.{ticket}"],
                                           origin=None, max_size=None)
        self.reader = asyncio.create_task(self._read())
        return ident

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("id") in self.pending:
                fut = self.pending.pop(msg["id"])
                if msg.get("error"):
                    fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
            elif msg.get("method") == "event":
                self.events.append({"t": time.time(), **(msg.get("params") or {})})

    async def call(self, method: str, params: dict, timeout: float = 60):
        rid = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout)

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.reader:
            self.reader.cancel()


def event_kinds(events: list[dict]) -> list[str]:
    return [str(e.get("type") or e.get("event") or e.get("kind") or "?") for e in events]


def assistant_texts(events: list[dict]) -> list[str]:
    texts = []
    for e in events:
        if (e.get("type") or e.get("event")) == "message.complete":
            p = e.get("payload") or e
            texts.append(str(p.get("text") or p.get("content") or p.get("message") or "")[:200])
    return texts


def mock_requests() -> list[dict]:
    try:
        return [json.loads(line) for line in open(MOCK_LOG)]
    except OSError:
        return []


def paseo_calls() -> list[dict]:
    try:
        return [json.loads(line) for line in open(PASEO_LOG)]
    except OSError:
        return []


def wait_for(pred, timeout: float, step: float = 0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


async def await_for(pred, timeout: float, step: float = 0.25):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(step)
    return pred()


# ---------------------------------------------------------------- paseo-mcp webhook

def post_event(event: dict, *, secret: str | None = None, port: int = 8645) -> tuple[int, dict]:
    sys.path.insert(0, PLUGIN_DIR)
    from paseo_bridge import signature  # the bridge's own signing code, as paseo-mcp signs
    if secret is None:
        secret = next(line.split("=", 1)[1].strip() for line in open(os.path.join(HERMES_HOME, ".env"))
                      if line.startswith("HERMES_PASEO_BRIDGE_SECRET="))
    did = f"dlv-{uuid.uuid4()}"
    body = json.dumps({"delivery_id": did, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "source": "paseo-mcp", "version": "0.4.0", "type": event["type"],
                       "summary": event.get("summary", ""), "event": event, "events": [event]}).encode()
    headers = {"Content-Type": "application/json", "Paseo-Mcp-Delivery": did,
               **signature.signing_headers("standard-webhooks", secret, did, body)}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/paseo/events", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def make_event(agent_id: str, labels: dict, seq: int, type_: str = "agent.turn_ended", **extra) -> dict:
    e = {"id": f"s5ep:{seq}", "epoch": "s5ep", "seq": seq, "type": type_,
         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agentId": agent_id, "title": "s5 demo",
         "status": "idle", "labels": labels, "cwd": "/home/agentbox/work/demo", "provider": "claude",
         "requiresAttention": True, "attentionReason": "finished", "summary": f'Agent "s5 demo" {type_}', "source": "hook"}
    if type_ == "agent.turn_ended":
        e["outcome"] = extra.pop("outcome", "finished")
        e["preview"] = {"text": f"S5 marker {seq}: all tests pass.", "untrusted": True}
    e.update(extra)
    return e


def updates_since(t0: float) -> list[dict]:
    """Mock-LLM requests made for an injected bridge notice after t0."""
    hits = []
    for r in mock_requests():
        if r["at"] < t0:
            continue
        users = [m for m in r.get("messages") or [] if m.get("role") == "user"]
        content = users[-1].get("content") if users else ""
        text = content if isinstance(content, str) else json.dumps(content)
        if "[Paseo agent update]" in text:
            hits.append({"at": r["at"], "first_user_message": _first_user(r), "notice": text[:400],
                         "messages": len(r.get("messages") or []), "answer": r["decision"]})
    return hits


def _first_user(r: dict) -> str:
    for m in r.get("messages") or []:
        if m.get("role") == "user":
            c = m.get("content")
            return (c if isinstance(c, str) else json.dumps(c))[:80]
    return ""


# ---------------------------------------------------------------- steps

async def step_turn(text: str, session_id: str | None, keep: float) -> None:
    gw = Gateway()
    ident = await gw.connect()
    if session_id:
        await gw.call("session.resume", {"session_id": session_id, "omit_messages": True})
    else:
        snap = await gw.call("session.create", {"source": "gui", "cwd": "/home/agentbox/work",
                                                "request_id": str(uuid.uuid4()),
                                                "client": {"name": "s5-harness", "version": "0"}})
        session_id = snap.get("session_id") or snap.get("stored_session_id")
        save_state(session_id=session_id, create_snapshot_keys=sorted(snap))
    t0 = time.time()
    res = await gw.call("prompt.submit", {"session_id": session_id, "text": text, "submission_id": str(uuid.uuid4())})
    await await_for(lambda: any(k in ("turn.end", "message.complete") for k in event_kinds(gw.events)), 60)
    await asyncio.sleep(keep)
    out(step="turn", session_id=session_id, submit=res, api_origin=ident.get("api_origin"),
        events=event_kinds(gw.events), assistant=assistant_texts(gw.events),
        llm_requests=len([r for r in mock_requests() if r["at"] >= t0]))
    await gw.close()


async def step_live_inject(session_id: str, agent_id: str, labels: dict, seq: int, wait: float) -> None:
    """Keep the session open in a client (as hermes-ui does), post an event, wait for the injected turn."""
    gw = Gateway()
    await gw.connect()
    snap = await gw.call("session.resume", {"session_id": session_id, "omit_messages": True})
    t0 = time.time()
    status, body = post_event(make_event(agent_id, labels, seq))
    hits = await await_for(lambda: [h for h in updates_since(t0) if f"marker {seq}" in h["notice"]], wait)
    await await_for(lambda: "message.complete" in event_kinds(gw.events), 15)
    out(step="live-inject", seq=seq, webhook=[status, body], injected=bool(hits),
        latency_s=round(hits[0]["at"] - t0, 2) if hits else None, llm=hits[:1],
        ws_events=event_kinds(gw.events), assistant=assistant_texts(gw.events),
        resume_keys=sorted(snap or {}))
    await gw.close()


def step_post(agent_id: str, labels: dict, seq: int, type_: str, outcome: str | None) -> None:
    extra = {"outcome": outcome} if outcome else {}
    status, body = post_event(make_event(agent_id, labels, seq, type_, **extra))
    out(step="post", seq=seq, type=type_, webhook=[status, body], at=time.time())


def step_wait_update(seq: int, since: float, wait: float) -> None:
    hits = wait_for(lambda: [h for h in updates_since(since) if f"marker {seq}" in h["notice"]], wait)
    out(step="wait-update", seq=seq, injected=bool(hits), llm=hits[:1])


def step_labels() -> None:
    calls = paseo_calls()
    out(step="labels", calls=calls)


def step_session_key(session_id: str) -> None:
    """The route key the gateway's session store holds for this session (for comparison with the label)."""
    import sqlite3
    db = sqlite3.connect(os.path.join(HERMES_HOME, "state.db"))
    rows = []
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in db.execute(f"PRAGMA table_info('{name}')")]
        if "session_key" in cols and "session_id" in cols:
            for r in db.execute(f"SELECT session_key, session_id FROM '{name}' WHERE session_id=?", (session_id,)):
                rows.append({"table": name, "session_key": r[0], "session_id": r[1]})
    out(step="session-key", rows=rows[:5])


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("turn")
    t.add_argument("text")
    t.add_argument("--session")
    t.add_argument("--keep", type=float, default=2)
    li = sub.add_parser("live-inject")
    li.add_argument("--session", required=True)
    li.add_argument("--agent", required=True)
    li.add_argument("--labels", required=True)
    li.add_argument("--seq", type=int, required=True)
    li.add_argument("--wait", type=float, default=20)
    p = sub.add_parser("post")
    p.add_argument("--agent", required=True)
    p.add_argument("--labels", required=True)
    p.add_argument("--seq", type=int, required=True)
    p.add_argument("--type", default="agent.turn_ended")
    p.add_argument("--outcome")
    w = sub.add_parser("wait-update")
    w.add_argument("--seq", type=int, required=True)
    w.add_argument("--since", type=float, required=True)
    w.add_argument("--wait", type=float, default=20)
    sub.add_parser("labels")
    sk = sub.add_parser("session-key")
    sk.add_argument("--session", required=True)
    a = ap.parse_args()
    if a.cmd == "turn":
        asyncio.run(step_turn(a.text, a.session, a.keep))
    elif a.cmd == "live-inject":
        asyncio.run(step_live_inject(a.session, a.agent, json.loads(a.labels), a.seq, a.wait))
    elif a.cmd == "post":
        step_post(a.agent, json.loads(a.labels), a.seq, a.type, a.outcome)
    elif a.cmd == "wait-update":
        step_wait_update(a.seq, a.since, a.wait)
    elif a.cmd == "labels":
        step_labels()
    elif a.cmd == "session-key":
        step_session_key(a.session)


if __name__ == "__main__":
    main()

"""Long-poll intake: ``paseo_wait_for_events`` over paseo-mcp's Streamable HTTP endpoint.

The alternative to the webhook receiver when nothing should post into Hermes: the bridge asks
paseo-mcp (plugin mode, ``http://127.0.0.1:<port>/mcp``, a read token) for events after a cursor. The
cursor is saved only after the events are stored, so a Hermes restart continues where it stopped and
loses nothing paseo-mcp still retains (at-least-once; the store drops repeats by event id). A paseo-mcp
restart starts a new epoch: the call reports ``gap`` and the events in between are gone on both sides.

Standard library only: JSON-RPC over HTTP POST, answers as JSON or as an SSE stream.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .version import VERSION

log = logging.getLogger("hermes_paseo_bridge")

PROTOCOL_VERSION = "2025-06-18"
CURSOR_KEY = "poll_cursor"
WAIT_TOOL = "paseo_wait_for_events"


class McpError(Exception):
    """The endpoint refused the request or answered with an error."""


class SessionExpired(McpError):
    """paseo-mcp forgot the session (restart, idle timeout): initialize again."""


def _parse_sse(lines, want_id: int) -> dict | None:
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
    if data:
        try:
            msg = json.loads("\n".join(data))
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        except ValueError:
            pass
    return None


class McpHttpClient:
    """Just enough of an MCP Streamable HTTP client for one tool."""

    def __init__(self, url: str, token: str, *, opener: Callable[..., Any] = urllib.request.urlopen):
        self.url = url
        self.token = token
        self.opener = opener
        self.session_id: str | None = None
        self.protocol = PROTOCOL_VERSION
        self._ids = itertools.count(1)

    def _request(self, payload: dict, timeout: float) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"hermes-paseo-bridge/{VERSION}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["Mcp-Protocol-Version"] = self.protocol
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            resp = self.opener(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read(300).decode("utf-8", "replace")
            if exc.code == 404 and self.session_id:
                self.session_id = None
                raise SessionExpired("session not found") from None
            raise McpError(f"HTTP {exc.code}: {detail}") from None
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            if "id" not in payload:
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith("text/event-stream"):
                msg = _parse_sse(resp, payload["id"])
            else:
                body = resp.read()
                msg = json.loads(body) if body else None
        if not isinstance(msg, dict):
            raise McpError("no answer to the request")
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(str(err.get("message") or err))
        return msg.get("result")

    def initialize(self, timeout: float = 15) -> None:
        self.session_id = None
        result = self._request({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "hermes-paseo-bridge", "version": VERSION}},
        }, timeout) or {}
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._request({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)

    def call_tool(self, name: str, arguments: dict, timeout: float) -> Any:
        if not self.session_id:
            self.initialize()
        result = self._request({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}}, timeout) or {}
        text = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        if result.get("isError"):
            raise McpError(text or "tool error")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            raise McpError(f"{name} returned no JSON: {text[:200]}") from None


class Poller:
    """Background thread: wait for events, hand them to ``submit`` as one delivery, save the cursor."""

    def __init__(self, client: McpHttpClient, store: Any, submit: Callable[[str, dict], dict], *,
                 timeout_seconds: int = 25, types: tuple[str, ...] = (), ready: Callable[[], bool] = lambda: True):
        self.client = client
        self.store = store
        self.submit = submit
        self.timeout_seconds = timeout_seconds
        self.types = types
        self.ready = ready
        self.state = "not started"
        self.gaps = 0
        self.polled = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> int:
        """One wait call. Returns the number of events received."""
        cursor = self.store.get_value(CURSOR_KEY)
        args: dict[str, Any] = {"timeout_sec": self.timeout_seconds if cursor else 0, "limit": 100}
        if cursor:
            args["cursor"] = cursor
        if self.types:
            args["types"] = list(self.types)
        page = self.client.call_tool(WAIT_TOOL, args, timeout=self.timeout_seconds + 20)
        if not isinstance(page, dict) or not page.get("next_cursor"):
            raise McpError(f"{WAIT_TOOL} returned no next_cursor")
        if self._stop.is_set():
            return 0  # unloading: leave the cursor, the next process reads these events again
        events = [e for e in page.get("events") or [] if isinstance(e, dict)]
        if page.get("gap") and cursor:
            self.gaps += 1
            needing = [a.get("agentId") or a.get("id") for a in page.get("agents") or []
                       if isinstance(a, dict) and a.get("requiresAttention")]
            log.warning("paseo-bridge: paseo-mcp reports a gap after cursor %s (restart or cursor too old); "
                        "events in between are lost. Agents waiting for attention now: %s",
                        cursor, ", ".join(str(a) for a in needing) or "none")
        if events:
            first, last = events[0].get("id"), events[-1].get("id")
            self.submit(f"poll:{first}..{last}", {"events": events, "source": "paseo-mcp-poll"})
        self.store.set_value(CURSOR_KEY, str(page["next_cursor"]))
        self.polled += len(events)
        return len(events)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="paseo-bridge-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        warned = ""
        while not self._stop.is_set():
            if not self.ready():
                self.state = "waiting for the gateway"
                self._stop.wait(1.0)
                continue
            try:
                self.poll_once()
                self.state = f"polling {self.client.url}"
                backoff = 1.0
                warned = ""
            except SessionExpired:
                continue
            except Exception as exc:  # network, auth, tool error: keep trying, log once per kind
                msg = str(exc) or type(exc).__name__
                self.state = f"poll failing ({msg}); retrying"
                if msg != warned:
                    log.warning("paseo-bridge: polling %s failed: %s; retrying with backoff", self.client.url, msg)
                    warned = msg
                self.client.session_id = None
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

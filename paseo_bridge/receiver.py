"""Local HTTP endpoint for paseo-mcp webhooks.

POST <path>   signed delivery -> 202 stored, 200 duplicate or test, 401 bad signature, 400/413 bad body
GET  /healthz liveness and counters, no secrets
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import signature
from .config import Settings
from .dispatcher import Dispatcher
from .version import VERSION

log = logging.getLogger("hermes_paseo_bridge")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(settings: Settings, dispatcher: Dispatcher, secret: Callable[[], str],
                 status: Callable[[], dict]) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"hermes-paseo-bridge/{VERSION}"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:  # route http.server's access log to logging
            log.debug("paseo-bridge http: " + fmt, *args)

        def _reply(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/healthz":
                self._reply(200, {"ok": True, "version": VERSION, **status()})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != settings.path:
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._reply(400, {"error": "bad Content-Length"})
                return
            if length > settings.max_body_bytes:
                self._reply(413, {"error": "body too large"})
                return
            body = self.rfile.read(length)
            headers = {k: v for k, v in self.headers.items()}
            key = secret()
            if not key:
                log.error("paseo-bridge: delivery refused, %s is not set", settings.secret_env)
                self._reply(503, {"error": "receiver has no secret configured"})
                return
            if not signature.verify(settings.signature, key, headers, body, tolerance=settings.tolerance_seconds):
                log.warning("paseo-bridge: delivery with an invalid or stale %s signature refused (from %s)",
                            settings.signature, self.client_address[0])
                self._reply(401, {"error": "invalid signature"})
                return
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except ValueError:
                self._reply(400, {"error": "body is not a JSON object"})
                return
            if payload.get("type") == "webhook.test":
                log.info("paseo-bridge: test delivery %s received", signature.delivery_id(headers, payload))
                self._reply(200, {"ok": True, "test": True})
                return
            did = signature.delivery_id(headers, payload)
            try:
                result = dispatcher.submit(did, payload)
            except Exception:
                log.exception("paseo-bridge: storing delivery %s failed", did)
                self._reply(500, {"error": "could not store the delivery"})  # paseo-mcp retries
                return
            self._reply(200 if result.get("duplicate") else 202, {"ok": True, **result})

    return Handler


class Receiver:
    def __init__(self, settings: Settings, dispatcher: Dispatcher, secret: Callable[[], str],
                 status: Callable[[], dict]):
        self.settings = settings
        self._handler = make_handler(settings, dispatcher, secret, status)
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> Optional[tuple[str, int]]:
        return self._server.server_address[:2] if self._server else None

    def start(self) -> None:
        """Bind and serve in a daemon thread. Raises OSError when the port is taken."""
        self._server = _Server((self.settings.listen_host, self.settings.listen_port), self._handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="paseo-bridge-http", daemon=True)
        self._thread.start()
        host, port = self.address
        log.info("paseo-bridge: listening on http://%s:%d%s", host, port, self.settings.path)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

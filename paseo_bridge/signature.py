"""Webhook signatures paseo-mcp sends, checked over the raw body.

standard-webhooks  webhook-id, webhook-timestamp, webhook-signature: v1,<base64 HMAC-SHA256(key, "<id>.<t>.<body>")>
                   key = base64 after "whsec_", otherwise the secret's UTF-8 bytes (standardwebhooks.com)
paseo              Paseo-Mcp-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import Mapping


def _header(headers: Mapping[str, str], name: str) -> str:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return ""


def standard_webhooks_key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[6:], validate=True)
        except (binascii.Error, ValueError):
            return b""
    return secret.encode("utf-8")


def standard_webhooks_signature(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(standard_webhooks_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def paseo_signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _fresh(timestamp: str, tolerance: int, now: float | None) -> int | None:
    try:
        t = int(timestamp)
    except (TypeError, ValueError):
        return None
    if abs((time.time() if now is None else now) - t) > tolerance:
        return None
    return t


def verify(scheme: str, secret: str, headers: Mapping[str, str], body: bytes, *,
           tolerance: int = 300, now: float | None = None) -> bool:
    """True when the request carries a valid, recent signature of ``body`` for ``scheme``."""
    if not secret:
        return False
    if scheme == "standard-webhooks":
        msg_id = _header(headers, "webhook-id") or _header(headers, "svix-id")
        ts = _header(headers, "webhook-timestamp") or _header(headers, "svix-timestamp")
        sigs = _header(headers, "webhook-signature") or _header(headers, "svix-signature")
        t = _fresh(ts, tolerance, now)
        if not msg_id or not sigs or t is None or not standard_webhooks_key(secret):
            return False
        expected = standard_webhooks_signature(secret, msg_id, t, body).encode()
        return any(hmac.compare_digest(entry.strip().encode(), expected) for entry in sigs.split(" ") if entry)
    if scheme == "paseo":
        parts = dict(p.strip().split("=", 1) for p in _header(headers, "Paseo-Mcp-Signature").split(",") if "=" in p)
        t = _fresh(parts.get("t", ""), tolerance, now)
        if t is None or not parts.get("v1"):
            return False
        return hmac.compare_digest(paseo_signature(secret, t, body).encode(), f"t={t},v1={parts['v1']}".encode())
    return False


def signing_headers(scheme: str, secret: str, msg_id: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    """Headers that sign ``body`` (used when the bridge forwards an event to a fallback URL)."""
    t = int(time.time()) if timestamp is None else timestamp
    if scheme == "paseo":
        return {"Paseo-Mcp-Signature": paseo_signature(secret, t, body), "Paseo-Mcp-Delivery": msg_id}
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(t),
        "webhook-signature": standard_webhooks_signature(secret, msg_id, t, body),
    }


def delivery_id(headers: Mapping[str, str], body: Mapping) -> str:
    """The id that stays the same on every retry of one delivery."""
    return (
        _header(headers, "webhook-id")
        or _header(headers, "svix-id")
        or _header(headers, "Paseo-Mcp-Delivery")
        or str(body.get("delivery_id") or "")
    )

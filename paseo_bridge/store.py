"""Durable bookkeeping in one SQLite file: which deliveries and events were seen (so a retry or a
second copy never makes a second turn), which events still wait for their turn (so a gateway restart
inside the coalescing window loses nothing), old->new session ids after a compaction, which session
last steered an agent, and the long-poll cursor."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY,
  received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  delivery_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  target TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  received_at REAL NOT NULL,
  done_at REAL
);
CREATE INDEX IF NOT EXISTS events_status ON events(status);
CREATE TABLE IF NOT EXISTS aliases (
  old_key TEXT PRIMARY KEY,
  new_key TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS routes (
  agent_id TEXT PRIMARY KEY,
  session_key TEXT NOT NULL,
  profile TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  at REAL NOT NULL
);
"""

MIN_ROUTE_PREFIX = 6

PENDING = "pending"
INJECTED = "injected"
FALLBACK = "fallback"
SKIPPED = "skipped"


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    delivery_id: str
    agent_id: str
    target: str
    event: dict
    received_at: float


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ intake

    def accept(self, delivery_id: str, events: Iterable[tuple[str, str, str, dict, str, str | None]],
               now: float | None = None) -> tuple[bool, list[str]]:
        """Record one delivery and its events in one transaction.

        ``events``: (event_id, agent_id, target, event, status, reason). Returns (duplicate delivery,
        ids of events that were new). An event already stored (same id from another delivery) is
        skipped, so it never gets a second turn."""
        t = time.time() if now is None else now
        with self._lock:
            cur = self._db.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                if delivery_id and cur.execute("SELECT 1 FROM deliveries WHERE id=?", (delivery_id,)).fetchone():
                    cur.execute("COMMIT")
                    return True, []
                fresh: list[str] = []
                for event_id, agent_id, target, event, status, reason in events:
                    done = None if status == PENDING else t
                    cur.execute(
                        "INSERT OR IGNORE INTO events(event_id, delivery_id, agent_id, target, payload, status, reason,"
                        " received_at, done_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (event_id, delivery_id, agent_id, target, json.dumps(event), status, reason, t, done),
                    )
                    if cur.rowcount:
                        fresh.append(event_id)
                if delivery_id:
                    cur.execute("INSERT INTO deliveries(id, received_at) VALUES (?,?)", (delivery_id, t))
                cur.execute("COMMIT")
                return False, fresh
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def pending(self) -> list[StoredEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, delivery_id, agent_id, target, payload, received_at FROM events"
                " WHERE status=? ORDER BY received_at, rowid", (PENDING,),
            ).fetchall()
        return [StoredEvent(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) for r in rows]

    def get(self, event_id: str) -> StoredEvent | None:
        with self._lock:
            r = self._db.execute(
                "SELECT event_id, delivery_id, agent_id, target, payload, received_at FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return StoredEvent(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5]) if r else None

    def mark(self, event_ids: Iterable[str], status: str, reason: str | None = None, now: float | None = None) -> None:
        t = time.time() if now is None else now
        ids = list(event_ids)
        with self._lock:
            self._db.executemany(
                "UPDATE events SET status=?, reason=?, done_at=? WHERE event_id=?",
                [(status, reason, t, i) for i in ids],
            )

    def status_of(self, event_id: str) -> tuple[str, str | None] | None:
        with self._lock:
            r = self._db.execute("SELECT status, reason FROM events WHERE event_id=?", (event_id,)).fetchone()
        return (r[0], r[1]) if r else None

    # ------------------------------------------------------------------ aliases

    def add_alias(self, old_key: str, new_key: str, now: float | None = None) -> None:
        if not old_key or not new_key or old_key == new_key:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO aliases(old_key, new_key, at) VALUES (?,?,?)",
                (old_key, new_key, time.time() if now is None else now),
            )

    def resolve(self, key: str, depth: int = 16) -> str:
        """Follow old->new links (a session id that compaction replaced) to the current key."""
        seen = {key}
        with self._lock:
            for _ in range(depth):
                r = self._db.execute("SELECT new_key FROM aliases WHERE old_key=?", (key,)).fetchone()
                if not r or r[0] in seen:
                    break
                key = r[0]
                seen.add(key)
        return key

    # ------------------------------------------------------------------ routes (steering)

    def set_route(self, agent_id: str, session_key: str, profile: str = "", now: float | None = None) -> None:
        """The session that last steered ``agent_id`` gets its next events (wins over its labels)."""
        if not agent_id or not session_key:
            return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO routes(agent_id, session_key, profile, at) VALUES (?,?,?,?)",
                (agent_id, session_key, profile, time.time() if now is None else now),
            )

    def route(self, agent_id: str) -> tuple[str, str] | None:
        """(session_key, profile) of the session that last steered the agent, or None.

        paseo-mcp accepts an id prefix in ``paseo_send_message``, so a stored prefix of at least
        ``MIN_ROUTE_PREFIX`` characters matches the full id in events (the longest, then newest, wins)."""
        with self._lock:
            r = self._db.execute("SELECT session_key, profile FROM routes WHERE agent_id=?", (agent_id,)).fetchone()
            if r is None:
                r = self._db.execute(
                    "SELECT session_key, profile FROM routes WHERE length(agent_id) >= ?"
                    " AND substr(?, 1, length(agent_id)) = agent_id"
                    " ORDER BY length(agent_id) DESC, at DESC LIMIT 1", (MIN_ROUTE_PREFIX, agent_id),
                ).fetchone()
        return (r[0], r[1]) if r else None

    # ------------------------------------------------------------------ key/value (poll cursor)

    def get_value(self, key: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_value(self, key: str, value: str, now: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv(key, value, at) VALUES (?,?,?)",
                             (key, value, time.time() if now is None else now))

    # ------------------------------------------------------------------ upkeep

    def prune(self, retention_seconds: float, now: float | None = None) -> int:
        """Forget finished events, deliveries and aliases older than the retention. Pending events stay."""
        cutoff = (time.time() if now is None else now) - retention_seconds
        with self._lock:
            n = self._db.execute("DELETE FROM events WHERE status != ? AND received_at < ?", (PENDING, cutoff)).rowcount
            self._db.execute("DELETE FROM deliveries WHERE received_at < ?", (cutoff,))
            self._db.execute("DELETE FROM aliases WHERE at < ?", (cutoff,))
            self._db.execute("DELETE FROM routes WHERE at < ?", (cutoff,))
        return n

    def counts(self) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT status, count(*) FROM events GROUP BY status").fetchall()
            last = self._db.execute(
                "SELECT event_id, agent_id, reason, done_at FROM events WHERE status=? ORDER BY done_at DESC LIMIT 5",
                (FALLBACK,),
            ).fetchall()
            deliveries = self._db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        return {
            "events": {s: n for s, n in rows},
            "deliveries": deliveries,
            "recent_fallbacks": [
                {"event_id": e, "agent_id": a, "reason": r, "at": d} for e, a, r, d in last
            ],
        }

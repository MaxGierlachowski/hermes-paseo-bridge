"""From stored events to turns: filter, coalesce per session, rate-limit, inject, fall back.

Every event that passes the filter is stored as ``pending`` before the HTTP request is answered. A
worker thread groups pending events by target session, waits ``coalesce_seconds`` after the last one
(at most four windows after the first), and injects one message per group. Whatever cannot reach its
session goes to the configured fallback, and the reason is logged and stored with the event."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import messages, signature
from .config import FallbackSettings, Settings
from .store import FALLBACK, INJECTED, PENDING, SKIPPED, Store, StoredEvent
from .version import VERSION

log = logging.getLogger("hermes_paseo_bridge")

FALLBACK_TARGET = "fallback"
SESSION_PREFIX = "session:"
RETRY_LATER_SECONDS = 10.0

# inject(session_key, text) -> (True, "") accepted | (False, reason) refused | (None, reason) try again later
Injector = Callable[[str, str], "tuple[Optional[bool], str]"]


def event_key(event: dict, delivery_id: str, index: int) -> str:
    """paseo-mcp event ids ("<epoch>:<seq>") are unique per paseo-mcp process; fall back to the delivery."""
    eid = event.get("id")
    return f"evt:{eid}" if eid else f"dlv:{delivery_id}:{index}"


@dataclass
class Batch:
    target: str
    event_ids: list[str] = field(default_factory=list)
    first_at: float = 0.0
    due_at: float = 0.0
    reason: str = ""


class Fallback:
    """What happens to events that cannot reach the session that started their agent."""

    def __init__(self, settings: FallbackSettings, inject: Injector, render: Callable[[list[dict], str], str], *,
                 getenv: Callable[[str], Optional[str]] = os.environ.get,
                 post: Optional[Callable[[str, bytes, dict, float], int]] = None):
        self.settings = settings
        self.inject = inject
        self.render = render
        self.getenv = getenv
        self.post = post or _http_post

    def handle(self, items: list[StoredEvent], reason: str) -> "tuple[Optional[bool], str]":
        mode = self.settings.mode
        agents = sorted({i.agent_id for i in items})
        if mode == "log":
            log.warning("paseo-bridge fallback (log): %d event(s) of agent(s) %s not injected: %s",
                        len(items), ", ".join(agents), reason)
            return True, "logged"
        if mode == "session":
            note = f"These agents could not reach the session that started them ({reason})."
            ok, why = self.inject(self.settings.session_key, self.render([i.event for i in items], note))
            if ok:
                log.warning("paseo-bridge fallback (session): %d event(s) of agent(s) %s sent to %s: %s",
                            len(items), ", ".join(agents), self.settings.session_key, reason)
                return True, f"sent to fallback session {self.settings.session_key}"
            if ok is None:
                return None, why
            log.error("paseo-bridge fallback session %s refused too (%s); events of %s only logged: %s",
                      self.settings.session_key, why, ", ".join(agents), reason)
            return False, f"fallback session refused: {why}"
        return self._forward(items, reason)

    def _forward(self, items: list[StoredEvent], reason: str) -> "tuple[Optional[bool], str]":
        s = self.settings
        secret = self.getenv(s.secret_env) if s.secret_env else ""
        if s.secret_env and not secret:
            log.error("paseo-bridge fallback (forward): %s is not set; %d event(s) only logged: %s",
                      s.secret_env, len(items), reason)
            return False, f"forward secret {s.secret_env} not set"
        failures = []
        for item in items:
            e = item.event
            msg_id = f"bridge-{item.event_id}"
            body = json.dumps({
                "delivery_id": msg_id,
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "hermes-paseo-bridge",
                "version": VERSION,
                "type": e.get("type"),
                "summary": e.get("summary") or "",
                "event": e,
                "events": [e],
                "bridge": {"reason": reason},
            }, separators=(",", ":")).encode()
            headers = {"Content-Type": "application/json", "User-Agent": f"hermes-paseo-bridge/{VERSION}"}
            if secret:
                headers.update(signature.signing_headers(s.signature, secret, msg_id, body))
            status, error = 0, ""
            for attempt in range(s.attempts):
                try:
                    status = self.post(s.url, body, headers, s.timeout_seconds)
                    error = "" if 200 <= status < 300 else f"HTTP {status}"
                except Exception as exc:  # network error, timeout
                    status, error = 0, str(exc)
                if not error:
                    break
                time.sleep(min(2 ** attempt, 8))
            if error:
                failures.append(f"{item.event_id}: {error}")
        if failures:
            log.error("paseo-bridge fallback (forward) to %s failed for %d event(s): %s; reason was: %s",
                      s.url, len(failures), "; ".join(failures), reason)
            return False, "forward failed: " + "; ".join(failures)
        log.warning("paseo-bridge fallback (forward): %d event(s) of agent(s) %s forwarded to %s: %s",
                    len(items), ", ".join(sorted({i.agent_id for i in items})), s.url, reason)
        return True, f"forwarded to {s.url}"


def _http_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Dispatcher:
    def __init__(self, settings: Settings, store: Store, inject: Injector, *, own_profile: str = "",
                 clock: Callable[[], float] = time.time, fallback: Optional[Fallback] = None):
        self.settings = settings
        self.store = store
        self.inject = inject
        self.own_profile = own_profile
        self.clock = clock
        self.fallback = fallback or Fallback(settings.fallback, inject, self._render)
        self._batches: dict[str, Batch] = {}
        self._sent: dict[str, deque] = {}
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._last_prune = 0.0

    # ------------------------------------------------------------------ classification

    def classify(self, event: dict) -> "tuple[str, str, Optional[str]]":
        """(status, target, reason) for one event."""
        s = self.settings
        kind = event.get("type")
        if kind not in s.event_types:
            return SKIPPED, "", f"type {kind} not selected"
        if kind == "agent.turn_ended" and event.get("outcome") not in s.turn_outcomes:
            return SKIPPED, "", f"outcome {event.get('outcome')} not selected"
        labels = event.get("labels") or {}
        for name, value in s.filter_labels.items():
            if labels.get(name) != value:
                return SKIPPED, "", f"label {name}={labels.get(name)!r} does not match {value!r}"
        key = labels.get(s.session_key_label)
        if not key:
            return PENDING, FALLBACK_TARGET, f"agent has no {s.session_key_label} label"
        profile = labels.get(s.profile_label)
        if profile and self.own_profile and profile != self.own_profile:
            return PENDING, FALLBACK_TARGET, f"agent belongs to Hermes profile {profile}, this is {self.own_profile}"
        return PENDING, SESSION_PREFIX + key, None

    # ------------------------------------------------------------------ intake

    def submit(self, delivery_id: str, body: dict) -> dict:
        """Store a verified delivery and schedule its events. Safe to call again with the same delivery."""
        events = body.get("events")
        if not isinstance(events, list):
            events = [body["event"]] if isinstance(body.get("event"), dict) else []
        rows, reasons = [], {}
        for i, e in enumerate(events):
            if not isinstance(e, dict):
                continue
            status, target, reason = self.classify(e)
            eid = event_key(e, delivery_id, i)
            rows.append((eid, str(e.get("agentId") or ""), target or "-", e, status, reason))
            reasons[eid] = (status, target, reason)
        duplicate, fresh = self.store.accept(delivery_id, rows, now=self.clock())
        if duplicate:
            log.info("paseo-bridge: delivery %s already received, ignored", delivery_id)
            return {"duplicate": True, "queued": 0}
        queued = 0
        for eid in fresh:
            status, target, reason = reasons[eid]
            if status == PENDING:
                self._enqueue(eid, target, reason or "")
                queued += 1
            else:
                log.debug("paseo-bridge: skipped %s: %s", eid, reason)
        skipped_dupes = len(rows) - len(fresh)
        if skipped_dupes:
            log.info("paseo-bridge: delivery %s carried %d event(s) already received", delivery_id, skipped_dupes)
        return {"duplicate": False, "queued": queued, "skipped": len(fresh) - queued, "already_seen": skipped_dupes}

    def restore(self) -> int:
        """Re-schedule events a previous process stored but never handled."""
        items = self.store.pending()
        for item in items:
            reason = ""
            if item.target == FALLBACK_TARGET:
                reason = self.classify(item.event)[2] or "stored for fallback"
            self._enqueue(item.event_id, item.target, reason, at=self.clock())
        if items:
            log.info("paseo-bridge: restored %d pending event(s)", len(items))
        return len(items)

    def _enqueue(self, event_id: str, target: str, reason: str, at: Optional[float] = None) -> None:
        now = self.clock() if at is None else at
        window = self.settings.coalesce_seconds
        with self._cond:
            batch = self._batches.get(target)
            if batch is None:
                batch = self._batches[target] = Batch(target=target, first_at=now, reason=reason)
            if event_id not in batch.event_ids:
                batch.event_ids.append(event_id)
            batch.due_at = max(batch.due_at, min(now + window, batch.first_at + 4 * window))
            self._cond.notify_all()

    # ------------------------------------------------------------------ delivery

    def _render(self, events: list[dict], note: str = "") -> str:
        return messages.render(events, preview_chars=self.settings.preview_chars,
                               instructions=self.settings.instructions, note=note)

    def _rate_wait(self, key: str, now: float) -> float:
        sent = self._sent.setdefault(key, deque())
        while sent and now - sent[0] >= 60:
            sent.popleft()
        if len(sent) < self.settings.max_turns_per_minute:
            return 0.0
        return 60 - (now - sent[0])

    def process_due(self) -> float:
        """Handle every batch that is due; return seconds until the next one (or 60)."""
        now = self.clock()
        with self._cond:
            due = [b for b in self._batches.values() if b.due_at <= now]
            for b in due:
                del self._batches[b.target]
        for batch in due:
            self._deliver(batch)
        if now - self._last_prune > 3600:
            self._last_prune = now
            try:
                self.store.prune(self.settings.retention_hours * 3600, now=now)
            except Exception:
                log.warning("paseo-bridge: pruning the store failed", exc_info=True)
        with self._cond:
            nxt = min((b.due_at for b in self._batches.values()), default=now + 60)
        return max(0.0, nxt - self.clock())

    def _requeue(self, batch: Batch, at: float) -> None:
        with self._cond:
            existing = self._batches.get(batch.target)
            if existing:
                existing.event_ids[:0] = [e for e in batch.event_ids if e not in existing.event_ids]
                existing.due_at = min(existing.due_at, at)
            else:
                batch.due_at = at
                self._batches[batch.target] = batch
            self._cond.notify_all()

    def _deliver(self, batch: Batch) -> None:
        items = [i for i in (self.store.get(e) for e in batch.event_ids) if i is not None]
        items = [i for i in items if (self.store.status_of(i.event_id) or ("",))[0] == PENDING]
        if not items:
            return
        now = self.clock()
        ids = [i.event_id for i in items]
        if batch.target == FALLBACK_TARGET:
            self._to_fallback(batch, items, batch.reason or "no session to continue")
            return
        key = self.store.resolve(batch.target[len(SESSION_PREFIX):])
        wait = self._rate_wait(key, now)
        if wait > 0:
            log.info("paseo-bridge: session %s is over %d turns/min, %d event(s) wait %.0fs",
                     key, self.settings.max_turns_per_minute, len(ids), wait)
            self._requeue(batch, now + wait)
            return
        try:
            ok, why = self.inject(key, self._render([i.event for i in items]))
        except Exception as exc:
            ok, why = False, f"inject raised {exc!r}"
        if ok:
            self._sent[key].append(now)
            self.store.mark(ids, INJECTED, f"session {key}", now=now)
            log.info("paseo-bridge: %d event(s) of agent(s) %s injected into session %s",
                     len(ids), ", ".join(sorted({i.agent_id for i in items})), key)
            return
        if ok is None:
            log.info("paseo-bridge: no Hermes host to inject into yet (%s); %d event(s) stay pending", why, len(ids))
            self._requeue(batch, now + RETRY_LATER_SECONDS)
            return
        self._to_fallback(batch, items, f"session {key}: {why}")

    def _to_fallback(self, batch: Batch, items: list[StoredEvent], reason: str) -> None:
        now = self.clock()
        if self.settings.fallback.mode == "session":
            wait = self._rate_wait(FALLBACK_TARGET, now)
            if wait > 0:
                self._requeue(batch, now + wait)
                return
        try:
            ok, detail = self.fallback.handle(items, reason)
        except Exception as exc:
            ok, detail = False, f"fallback raised {exc!r}"
        if ok is None:
            self._requeue(batch, now + RETRY_LATER_SECONDS)
            return
        if ok and self.settings.fallback.mode == "session":
            self._sent.setdefault(FALLBACK_TARGET, deque()).append(now)
        self.store.mark([i.event_id for i in items], FALLBACK, f"{reason} -> {detail}", now=now)

    # ------------------------------------------------------------------ worker

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="paseo-bridge-dispatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            try:
                wait = self.process_due()
            except Exception:
                log.exception("paseo-bridge: dispatch loop error")
                wait = 5.0
            with self._cond:
                if self._stopping:
                    return
                self._cond.wait(timeout=max(0.05, min(wait, 60.0)))
                if self._stopping:
                    return

    def pending_targets(self) -> dict[str, int]:
        with self._cond:
            return {t: len(b.event_ids) for t, b in self._batches.items()}

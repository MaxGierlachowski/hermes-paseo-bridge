import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from paseo_bridge.config import Settings  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeInjector:
    """Records inject calls; ``results`` maps session keys to (ok, reason)."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, key, text):
        self.calls.append((key, text))
        return self.results.get(key, (True, ""))


def event(seq=1, agent="agent-1", type_="agent.turn_ended", outcome="finished", labels=None, **extra):
    base_labels = {"origin": "hermes", "hermes_session_key": "agent:main:slack:dm:T1:D1:1.0", "hermes_profile": "default"}
    if labels is not None:
        base_labels = labels
    e = {
        "id": f"ep1:{seq}", "epoch": "ep1", "seq": seq, "type": type_, "at": "2026-11-18T10:00:00Z",
        "agentId": agent, "title": "fix login", "status": "idle", "labels": base_labels, "cwd": "/work",
        "provider": "claude", "requiresAttention": True, "attentionReason": "finished",
        "summary": f'Agent "fix login" {type_}', "source": "hook",
    }
    if type_ == "agent.turn_ended":
        e["outcome"] = outcome
    e.update(extra)
    return e


def delivery(*events, delivery_id="dlv-1"):
    return {"delivery_id": delivery_id, "sent_at": "2026-11-18T10:00:00Z", "source": "paseo-mcp", "version": "0.4.0",
            "type": events[-1]["type"], "summary": "", "event": events[-1], "events": list(events)}


def settings(**kw):
    return Settings(**kw)

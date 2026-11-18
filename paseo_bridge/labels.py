"""Labels that tie a new Paseo agent to the Hermes session that starts it."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Mapping

from .config import Settings

MAX_LABEL_VALUE = 512


@dataclass(frozen=True)
class SessionRef:
    """What the bridge knows about the calling session at tool-call time."""

    session_key: str
    profile: str = ""
    session_id: str = ""
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""

    def value(self, name: str) -> str:
        return str(getattr(self, name, "") or "")


def tool_matches(tool_name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool_name, p) for p in patterns)


def bridge_labels(existing: Any, session: SessionRef, settings: Settings) -> dict[str, str] | None:
    """The full ``labels`` argument for a create call, or None when there is no session to point at.

    The bridge's own labels (session key, profile, session id, platform) always win: a value the model
    typed could point at another chat. ``extra_labels`` only fill gaps, so a value the model passed on
    purpose stays. Labels already present (``todo``, ``card``, ``kind`` ...) are kept unchanged."""
    if not session.session_key:
        return None
    labels: dict[str, str] = {}
    if isinstance(existing, Mapping):
        labels.update({str(k): str(v) for k, v in existing.items() if v is not None})
    p = settings.label_prefix
    own = {
        f"{p}session_key": session.session_key,
        f"{p}profile": session.profile,
        f"{p}session_id": session.session_id,
        f"{p}platform": session.platform,
    }
    for name, value in own.items():
        if value:
            labels[name] = value[:MAX_LABEL_VALUE]
    for name, field_name in settings.extra_labels.items():
        value = session.value(field_name)
        if value and not labels.get(name):
            labels[name] = value[:MAX_LABEL_VALUE]
    return labels

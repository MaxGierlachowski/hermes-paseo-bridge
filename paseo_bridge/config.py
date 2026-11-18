"""Settings of the bridge, read from ``plugins.entries.hermes-paseo-bridge.settings`` in the
profile's ``config.yaml``. Every value has a default, so an empty ``settings`` block works."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

SIGNATURE_SCHEMES = ("standard-webhooks", "paseo")
FALLBACK_MODES = ("log", "forward", "session")
LISTENER_MODES = ("auto", "always", "never")

DEFAULT_EXTRA_LABELS = {
    # paseo-mcp's documented "who started it" label, plus the chat and thread of a messaging session.
    # Filled only when the model did not pass a value itself.
    "origin_ref": "session_id",
    "hermes_chat_id": "chat_id",
    "hermes_thread_id": "thread_id",
}

DEFAULT_INSTRUCTIONS = (
    "Read the full result with paseo_read_timeline before you rely on it. Then carry on with what you "
    "started the agent for, or tell the user what happened. If nothing is left to do, say so in one line."
)


class SettingsError(ValueError):
    """A setting has a value the bridge cannot use."""


@dataclass(frozen=True)
class FallbackSettings:
    mode: str = "log"
    url: str = ""
    secret_env: str = ""
    signature: str = "standard-webhooks"
    session_key: str = ""
    timeout_seconds: float = 10.0
    attempts: int = 3


@dataclass(frozen=True)
class Settings:
    # Labels on new agents
    tools: tuple[str, ...] = ("mcp__*__paseo_create_agent",)
    label_prefix: str = "hermes_"
    extra_labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EXTRA_LABELS))
    # Event intake
    listener: str = "auto"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8645
    path: str = "/paseo/events"
    secret_env: str = "HERMES_PASEO_BRIDGE_SECRET"
    signature: str = "standard-webhooks"
    tolerance_seconds: int = 300
    max_body_bytes: int = 1_048_576
    # Which events become a turn
    filter_labels: dict[str, str] = field(default_factory=lambda: {"origin": "hermes"})
    event_types: tuple[str, ...] = ("agent.turn_ended", "agent.permission_requested")
    turn_outcomes: tuple[str, ...] = ("finished", "error")
    # Pacing
    coalesce_seconds: float = 5.0
    max_turns_per_minute: int = 6
    # Message
    preview_chars: int = 600
    instructions: str = DEFAULT_INSTRUCTIONS
    # Bookkeeping
    retention_hours: float = 72.0
    fallback: FallbackSettings = field(default_factory=FallbackSettings)

    @property
    def session_key_label(self) -> str:
        return f"{self.label_prefix}session_key"

    @property
    def profile_label(self) -> str:
        return f"{self.label_prefix}profile"


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
        raise SettingsError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _str_dict(value: Any, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise SettingsError(f"{name} must map strings to strings")
    return dict(value)


def _number(value: Any, name: str, *, minimum: float, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError(f"{name} must be a number")
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}")
    return int(value) if integer else float(value)


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise SettingsError(f"{name} must be one of {', '.join(choices)}")
    return value


def load_settings(get: Callable[[str, Any], Any]) -> Settings:
    """Build Settings from a ``get(key, default)`` reader (``ctx.get_config`` in Hermes)."""
    d = Settings()
    fb_raw = get("fallback", None) or {}
    if not isinstance(fb_raw, dict):
        raise SettingsError("fallback must be a mapping")
    fd = FallbackSettings()
    fallback = FallbackSettings(
        mode=_choice(fb_raw.get("mode", fd.mode), "fallback.mode", FALLBACK_MODES),
        url=str(fb_raw.get("url", fd.url) or ""),
        secret_env=str(fb_raw.get("secret_env", fd.secret_env) or ""),
        signature=_choice(fb_raw.get("signature", fd.signature), "fallback.signature", SIGNATURE_SCHEMES),
        session_key=str(fb_raw.get("session_key", fd.session_key) or ""),
        timeout_seconds=_number(fb_raw.get("timeout_seconds", fd.timeout_seconds), "fallback.timeout_seconds", minimum=1),
        attempts=_number(fb_raw.get("attempts", fd.attempts), "fallback.attempts", minimum=1, integer=True),
    )
    if fallback.mode == "forward" and not fallback.url:
        raise SettingsError("fallback.mode forward needs fallback.url")
    if fallback.mode == "session" and not fallback.session_key:
        raise SettingsError("fallback.mode session needs fallback.session_key")

    path = str(get("path", d.path))
    if not path.startswith("/"):
        raise SettingsError("path must start with /")
    prefix = str(get("label_prefix", d.label_prefix))
    if not prefix:
        raise SettingsError("label_prefix must not be empty")
    return Settings(
        tools=_str_tuple(get("tools", list(d.tools)), "tools"),
        label_prefix=prefix,
        extra_labels=_str_dict(get("extra_labels", d.extra_labels), "extra_labels"),
        listener=_choice(get("listener", d.listener), "listener", LISTENER_MODES),
        listen_host=str(get("listen_host", d.listen_host)),
        listen_port=_number(get("listen_port", d.listen_port), "listen_port", minimum=0, integer=True),
        path=path,
        secret_env=str(get("secret_env", d.secret_env)),
        signature=_choice(get("signature", d.signature), "signature", SIGNATURE_SCHEMES),
        tolerance_seconds=_number(get("tolerance_seconds", d.tolerance_seconds), "tolerance_seconds", minimum=1, integer=True),
        max_body_bytes=_number(get("max_body_bytes", d.max_body_bytes), "max_body_bytes", minimum=1024, integer=True),
        filter_labels=_str_dict(get("filter_labels", d.filter_labels), "filter_labels"),
        event_types=_str_tuple(get("event_types", list(d.event_types)), "event_types"),
        turn_outcomes=_str_tuple(get("turn_outcomes", list(d.turn_outcomes)), "turn_outcomes"),
        coalesce_seconds=_number(get("coalesce_seconds", d.coalesce_seconds), "coalesce_seconds", minimum=0),
        max_turns_per_minute=_number(get("max_turns_per_minute", d.max_turns_per_minute), "max_turns_per_minute", minimum=1, integer=True),
        preview_chars=_number(get("preview_chars", d.preview_chars), "preview_chars", minimum=0, integer=True),
        instructions=str(get("instructions", d.instructions)),
        retention_hours=_number(get("retention_hours", d.retention_hours), "retention_hours", minimum=1),
        fallback=fallback,
    )

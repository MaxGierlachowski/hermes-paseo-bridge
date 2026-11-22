"""Settings of the bridge, read from ``plugins.entries.hermes-paseo-bridge.settings`` in the
profile's ``config.yaml``, with ``HERMES_PASEO_BRIDGE_<KEY>`` environment variables on top (see
``ENV_OVERRIDES``). Every value has a default, so an empty ``settings`` block works. Secrets are never
settings: the settings only name the environment variables that hold them."""
from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SIGNATURE_SCHEMES = ("standard-webhooks", "paseo")
FALLBACK_MODES = ("log", "home", "forward", "session")
LISTENER_MODES = ("auto", "always", "never")
ENV_PREFIX = "HERMES_PASEO_BRIDGE_"

# Settings an environment variable may set (``HERMES_PASEO_BRIDGE_<KEY upper>``), e.g. rendered by an
# installer into the gateway's environment. Lists and mappings take JSON. ``fallback.*`` keys use
# ``HERMES_PASEO_BRIDGE_FALLBACK_<KEY>``.
ENV_OVERRIDES = (
    "listener", "listen_host", "listen_port", "path", "secret_env", "signature",
    "paseo_mcp_url", "paseo_mcp_token_env", "poll_timeout_seconds",
    "tools", "steer_tools", "filter_labels", "event_types", "turn_outcomes",
    "coalesce_seconds", "max_turns_per_minute", "preview_chars", "retention_hours",
)
FALLBACK_ENV_OVERRIDES = ("mode", "platform", "url", "secret_env", "signature", "session_key")

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
    # home: the profile's messaging home channel (``platform`` picks one; else the first configured),
    # and the log when there is none.
    mode: str = "home"
    platform: str = ""
    url: str = ""
    secret_env: str = ""
    signature: str = "standard-webhooks"
    session_key: str = ""
    timeout_seconds: float = 10.0
    attempts: int = 3


@dataclass(frozen=True)
class Settings:
    # Labels on new agents; steering calls re-route an existing agent's events to the calling session
    tools: tuple[str, ...] = ("mcp__*__paseo_create_agent",)
    steer_tools: tuple[str, ...] = ("mcp__*__paseo_send_message",)
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
    # Event intake by long-poll (paseo_wait_for_events over paseo-mcp's Streamable HTTP endpoint)
    paseo_mcp_url: str = ""
    paseo_mcp_token_env: str = "HERMES_PASEO_BRIDGE_PASEO_MCP_TOKEN"
    poll_timeout_seconds: int = 25
    # Which events become a turn
    filter_labels: dict[str, str] = field(default_factory=lambda: {"origin": "hermes"})
    event_types: tuple[str, ...] = ("agent.turn_ended", "agent.permission_requested", "agent.closed")
    turn_outcomes: tuple[str, ...] = ("finished", "error", "limit")
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


def _env_value(raw: str, default: Any) -> Any:
    """An environment string as the type of the setting's default (JSON for lists and mappings)."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return raw  # validated (and refused) below
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    if isinstance(default, (list, tuple, dict)):
        try:
            return json.loads(raw)
        except ValueError:
            return [p.strip() for p in raw.split(",") if p.strip()] if isinstance(default, (list, tuple)) else raw
    return raw


def with_env(get: Callable[[str, Any], Any], environ: Mapping[str, str] | None = None) -> Callable[[str, Any], Any]:
    """Wrap a settings reader so ``HERMES_PASEO_BRIDGE_<KEY>`` variables win over config.yaml."""
    env = os.environ if environ is None else environ

    def read(key: str, default: Any) -> Any:
        if key == "fallback":
            value = get(key, default)
            merged = dict(value) if isinstance(value, dict) else ({} if value is None else value)
            if isinstance(merged, dict):
                fd = FallbackSettings()
                for sub in FALLBACK_ENV_OVERRIDES:
                    raw = env.get(f"{ENV_PREFIX}FALLBACK_{sub.upper()}")
                    if raw is not None and raw != "":
                        merged[sub] = _env_value(raw, getattr(fd, sub))
            return merged
        if key in ENV_OVERRIDES:
            raw = env.get(ENV_PREFIX + key.upper())
            if raw is not None and raw != "":
                return _env_value(raw, default)
        return get(key, default)

    return read


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def load_settings(get: Callable[[str, Any], Any]) -> Settings:
    """Build Settings from a ``get(key, default)`` reader (``ctx.get_config`` in Hermes)."""
    d = Settings()
    fb_raw = get("fallback", None) or {}
    if not isinstance(fb_raw, dict):
        raise SettingsError("fallback must be a mapping")
    fd = FallbackSettings()
    fallback = FallbackSettings(
        mode=_choice(fb_raw.get("mode", fd.mode), "fallback.mode", FALLBACK_MODES),
        platform=str(fb_raw.get("platform", fd.platform) or ""),
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
    host = str(get("listen_host", d.listen_host))
    if not is_loopback(host):
        raise SettingsError("listen_host must be a loopback address (127.0.0.1, ::1 or localhost)")
    steer = get("steer_tools", list(d.steer_tools))
    mcp_url = str(get("paseo_mcp_url", d.paseo_mcp_url) or "")
    if mcp_url and not mcp_url.startswith(("http://", "https://")):
        raise SettingsError("paseo_mcp_url must be an http(s) URL")
    return Settings(
        tools=_str_tuple(get("tools", list(d.tools)), "tools"),
        steer_tools=_str_tuple(steer, "steer_tools") if steer else (),
        label_prefix=prefix,
        extra_labels=_str_dict(get("extra_labels", d.extra_labels), "extra_labels"),
        listener=_choice(get("listener", d.listener), "listener", LISTENER_MODES),
        listen_host=host,
        listen_port=_number(get("listen_port", d.listen_port), "listen_port", minimum=0, integer=True),
        path=path,
        secret_env=str(get("secret_env", d.secret_env)),
        signature=_choice(get("signature", d.signature), "signature", SIGNATURE_SCHEMES),
        tolerance_seconds=_number(get("tolerance_seconds", d.tolerance_seconds), "tolerance_seconds", minimum=1, integer=True),
        max_body_bytes=_number(get("max_body_bytes", d.max_body_bytes), "max_body_bytes", minimum=1024, integer=True),
        paseo_mcp_url=mcp_url,
        paseo_mcp_token_env=str(get("paseo_mcp_token_env", d.paseo_mcp_token_env) or ""),
        poll_timeout_seconds=_number(get("poll_timeout_seconds", d.poll_timeout_seconds), "poll_timeout_seconds",
                                     minimum=0, integer=True),
        filter_labels=_str_dict(get("filter_labels", d.filter_labels), "filter_labels"),
        event_types=_str_tuple(get("event_types", list(d.event_types)), "event_types"),
        turn_outcomes=_str_tuple(get("turn_outcomes", list(d.turn_outcomes)), "turn_outcomes"),
        coalesce_seconds=_number(get("coalesce_seconds", d.coalesce_seconds), "coalesce_seconds", minimum=0),
        max_turns_per_minute=_number(get("max_turns_per_minute", d.max_turns_per_minute), "max_turns_per_minute",
                                     minimum=1, integer=True),
        preview_chars=_number(get("preview_chars", d.preview_chars), "preview_chars", minimum=0, integer=True),
        instructions=str(get("instructions", d.instructions)),
        retention_hours=_number(get("retention_hours", d.retention_hours), "retention_hours", minimum=1),
        fallback=fallback,
    )

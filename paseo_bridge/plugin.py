"""Hermes wiring: the pre_tool_call hook that labels new agents, the receiver that takes their events,
and the injector that turns them into a turn of the session that started the agent."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import Settings, SettingsError, load_settings
from .dispatcher import Dispatcher
from .labels import SessionRef, bridge_labels, tool_matches
from .receiver import Receiver
from .store import Store
from .version import VERSION

log = logging.getLogger("hermes_paseo_bridge")

PLUGIN_NAME = "hermes-paseo-bridge"
BIND_RETRY_SECONDS = 15.0


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        return os.environ.get(name, "")


def current_session(profile: str, session_id: str = "") -> SessionRef:
    """The session the running tool call belongs to, read from Hermes' per-turn context.

    ``HERMES_SESSION_KEY`` is exactly the key ``inject_message(session_key=...)`` routes by: the gateway
    route (``agent:<profile>:<platform>:...``) for messaging chats and gateway-local Desktop/TUI
    sessions, the durable key for a standalone TUI host."""
    key = ""
    try:
        from gateway.session_context import bound_session_key
        key = bound_session_key()
    except Exception:
        pass
    key = key or _session_env("HERMES_SESSION_KEY")
    return SessionRef(
        session_key=key,
        profile=profile,
        session_id=session_id or _session_env("HERMES_SESSION_ID"),
        platform=_session_env("HERMES_SESSION_PLATFORM") or _session_env("HERMES_SESSION_SOURCE"),
        chat_id=_session_env("HERMES_SESSION_CHAT_ID"),
        thread_id=_session_env("HERMES_SESSION_THREAD_ID"),
    )


class Bridge:
    def __init__(self, ctx: Any, settings: Settings, store: Store):
        self.ctx = ctx
        self.settings = settings
        self.store = store
        self.profile = getattr(ctx, "profile_name", "") or ""
        self.dispatcher = Dispatcher(settings, store, self.inject, own_profile=self.profile)
        self.receiver = Receiver(settings, self.dispatcher, self.secret, self.status_counts)
        self._stop = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None
        self.listener_state = "not started"
        self.labelled = 0

    # ------------------------------------------------------------------ host access

    def _manager(self) -> Any:
        return getattr(self.ctx, "_manager", None)

    def host_ready(self) -> bool:
        """Is a gateway or TUI host live to take injected turns? If this Hermes does not expose the
        flags, assume yes and let inject_message decide (a refusal then goes to the fallback)."""
        m = self._manager()
        flags = ("has_gateway_message_injector", "has_tui_message_injector")
        if m is None or not any(hasattr(m, f) for f in flags):
            return True
        return any(bool(getattr(m, f, False)) for f in flags)

    def _session_known(self, key: str) -> Optional[bool]:
        """Best effort: is a gateway route key in the gateway's session store? None = cannot tell.

        Only for ``agent:...`` route keys; other keys belong to a TUI host and are left to inject_message."""
        if not key.startswith("agent:"):
            return None
        try:
            host = getattr(self._manager(), "_gateway_message_injector", None)
            store = getattr(host[0], "session_store", None) if host else None
            if store is None:
                return None
            return store.lookup_by_session_key(key) is not None
        except Exception:
            return None

    def inject(self, key: str, text: str) -> "tuple[Optional[bool], str]":
        if not self.host_ready():
            return None, "no live Hermes gateway or TUI host"
        if self._session_known(key) is False:
            return False, "no such session in the gateway's session store"
        ok = self.ctx.inject_message(text, role="user", session_key=key)
        if ok:
            return True, ""
        return False, "inject_message refused it (gateway.log names the reason; check allow_gateway_injection)"

    def secret(self) -> str:
        return os.environ.get(self.settings.secret_env, "") if self.settings.secret_env else ""

    # ------------------------------------------------------------------ hooks

    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any) -> Any:
        if not tool_matches(str(tool_name or ""), self.settings.tools):
            return None
        session = current_session(self.profile, str(kwargs.get("session_id") or ""))
        labels = bridge_labels((args or {}).get("labels") if isinstance(args, dict) else None, session, self.settings)
        if labels is None:
            log.info("paseo-bridge: %s called outside a session; agent gets no session label", tool_name)
            return None
        self.labelled += 1
        log.info("paseo-bridge: labelled new agent for session %s (%s)", session.session_key, tool_name)
        return {"action": "modify", "args": {"labels": labels}}

    def on_post_compaction(self, session_id: str = "", old_session_id: str = "", **kwargs: Any) -> None:
        if old_session_id and session_id and old_session_id != session_id:
            self.store.add_alias(old_session_id, session_id)

    # ------------------------------------------------------------------ listener

    def start(self) -> None:
        self.dispatcher.restore()
        self.dispatcher.start()
        if self.settings.listener == "never":
            self.listener_state = "off (listener: never)"
            return
        self._listener_thread = threading.Thread(target=self._listen, name="paseo-bridge-listen", daemon=True)
        self._listener_thread.start()

    def _listen(self) -> None:
        warned_wait = warned_bind = False
        while not self._stop.is_set():
            if self.settings.listener == "auto" and not self.host_ready():
                if not warned_wait:
                    self.listener_state = "waiting for the gateway"
                    warned_wait = True
                self._stop.wait(1.0)
                continue
            if not self.secret():
                self.listener_state = f"off: {self.settings.secret_env} is not set"
                log.error("paseo-bridge: %s is not set; not listening for Paseo events", self.settings.secret_env)
                return
            try:
                self.receiver.start()
                host, port = self.receiver.address
                self.listener_state = f"listening on http://{host}:{port}{self.settings.path}"
                return
            except OSError as exc:
                self.listener_state = f"cannot bind {self.settings.listen_host}:{self.settings.listen_port}: {exc}"
                if not warned_bind:
                    log.warning("paseo-bridge: %s; retrying every %.0fs", self.listener_state, BIND_RETRY_SECONDS)
                    warned_bind = True
                self._stop.wait(BIND_RETRY_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        self.receiver.stop()
        self.dispatcher.stop()
        self.store.close()

    # ------------------------------------------------------------------ status

    def status_counts(self) -> dict:
        c = self.store.counts()
        return {"events": c["events"], "deliveries": c["deliveries"], "waiting": self.dispatcher.pending_targets()}

    def status_text(self, raw_args: str = "", **kwargs: Any) -> str:
        c = self.store.counts()
        lines = [
            f"hermes-paseo-bridge {VERSION} (profile {self.profile or '?'})",
            f"Listener: {self.listener_state}",
            f"Injection host: {'ready' if self.host_ready() else 'none'}",
            f"Agents labelled since start: {self.labelled}",
            f"Deliveries kept: {c['deliveries']}; events: "
            + (", ".join(f"{k} {v}" for k, v in sorted(c["events"].items())) or "none"),
            f"Fallback mode: {self.settings.fallback.mode}",
        ]
        waiting = self.dispatcher.pending_targets()
        if waiting:
            lines.append("Waiting: " + ", ".join(f"{t} ({n})" for t, n in waiting.items()))
        for f in c["recent_fallbacks"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["at"] or 0))
            lines.append(f"Fallback {when} agent {f['agent_id']}: {f['reason']}")
        return "\n".join(lines)


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        path = home / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


def register(ctx: Any) -> None:
    try:
        settings = load_settings(lambda key, default: ctx.get_config(key, default=default))
    except SettingsError as exc:
        log.error("paseo-bridge: invalid settings (%s); plugin stays inactive", exc)
        return
    bridge = Bridge(ctx, settings, Store(_data_dir() / "bridge.db"))
    ctx.register_hook("pre_tool_call", bridge.on_pre_tool_call)
    ctx.register_hook("post_compaction", bridge.on_post_compaction)
    ctx.register_command("paseo-bridge", bridge.status_text, description="Status of the Paseo event bridge")
    bridge.start()
    ctx.on_unload(bridge.stop)

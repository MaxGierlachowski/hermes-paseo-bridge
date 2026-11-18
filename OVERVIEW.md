# Overview

How the plugin is put together, for contributors and reviewers. User documentation is in [README.md](README.md).

## Layout

| Path | What it does |
|---|---|
| `plugin.yaml` | Manifest: name, hooks, `config_schema` for the settings form. |
| `__init__.py` | Entry point Hermes imports; re-exports `register`. |
| `paseo_bridge/plugin.py` | The only module that touches Hermes: reads settings, registers the hooks and `/paseo-bridge`, reads the session from Hermes' per-turn context, injects turns, starts the receiver once a host is live. |
| `paseo_bridge/labels.py` | Which tool calls get labels, and the merged `labels` argument. |
| `paseo_bridge/receiver.py` | `ThreadingHTTPServer` on loopback: verifies the signature over the raw body, hands the delivery to the dispatcher, `/healthz`. |
| `paseo_bridge/signature.py` | Standard Webhooks and paseo-mcp's own scheme, verify and sign. |
| `paseo_bridge/store.py` | SQLite (WAL): deliveries, events with status and reason, compaction aliases, pruning. |
| `paseo_bridge/dispatcher.py` | Filter → store → coalesce per session → rate limit → inject → fallback. Worker thread. |
| `paseo_bridge/messages.py` | The text of an injected turn. |
| `paseo_bridge/config.py` | Settings with defaults and validation. |
| `tests/` | `unittest`, no Hermes needed. |
| `docs/` | GitHub Page. |

## Flow of one event

1. **Start.** The model calls `mcp__<server>__paseo_create_agent` (directly or through Hermes' deferred `tool_call`, which Hermes unwraps before hooks run). `pre_tool_call` returns `{"action": "modify", "args": {"labels": …}}`; Hermes shallow-merges it, so only `labels` changes.
2. **Event.** paseo-mcp sends a signed delivery: `{delivery_id, type, summary, event, events: [...]}`.
3. **Receive.** Signature and timestamp (±300 s) are checked; a `webhook.test` is acknowledged and dropped.
4. **Classify.** Per event: wrong type, outcome or label filter → `skipped`. No session key, or another profile's agent → pending for the fallback. Otherwise → pending for `session:<key>`.
5. **Store.** Delivery id and events are written in one transaction before the HTTP answer. A known delivery id → HTTP 200 `duplicate`; a known event id inside a new delivery is ignored.
6. **Coalesce.** Pending events wait per target `coalesce_seconds` after the newest one, at most four windows after the first.
7. **Rate limit.** At most `max_turns_per_minute` turns per session; the rest waits and is merged.
8. **Inject.** No live host → stays pending, retried every 10 s. A route key (`agent:…`) the gateway's session store does not know → fallback. Otherwise `ctx.inject_message(text, session_key=key)`; `False` → fallback.
9. **Mark.** `injected` or `fallback` with the reason; `/paseo-bridge` shows the last fallbacks.

## Hermes surfaces used

| Surface | Why |
|---|---|
| `ctx.register_hook("pre_tool_call")` | Add labels; `modify` is documented to merge into the tool arguments. |
| `ctx.register_hook("post_compaction")` | Follow a session id that compaction replaced. |
| `ctx.inject_message(session_key=…)` | Start or queue the turn; needs `allow_gateway_injection`. |
| `ctx.get_config`, `ctx.register_command`, `ctx.on_unload`, `ctx.profile_name` | Settings, status, cleanup, profile check. |
| `gateway.session_context.bound_session_key` / `get_session_env` | The calling session's route key and ids. |
| `plugins.plugin_storage.plugin_data_dir` | Where `bridge.db` lives. |
| `ctx._manager` (read only, guarded) | Whether a gateway or TUI host is live, and the gateway's session store for the unknown-key check. If these internals change, the plugin degrades instead of breaking: it treats the host as live (a refused injection goes to the fallback), and unknown keys are left to Hermes. |

## Design choices

- **Own receiver instead of a Hermes webhook route.** A route starts its own agent run; injection is what continues the original session. A loopback port keeps paseo-mcp's signed deliveries, retries and filters.
- **Push instead of polling `paseo_wait_for_events`.** Polling from a plugin needs `mcp_allowlist`, holds an MCP call open for minutes and loses the cursor when paseo-mcp restarts. Webhooks are retried by the sender, and the store makes them idempotent.
- **Labels in a hook instead of in paseo-mcp.** Hermes sends no session id to MCP servers (no `_meta`, one stdio process for all sessions), so only Hermes knows which session calls.
- **Store before answering.** A 202 means the event is on disk; a crash after that loses nothing.

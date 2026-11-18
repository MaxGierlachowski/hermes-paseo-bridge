# Changelog

## 0.1.0 (2026-11-18)

First release.

- `pre_tool_call` hook adds `hermes_session_key`, `hermes_profile`, `hermes_session_id`, `hermes_platform` (plus `origin_ref`, `hermes_chat_id`, `hermes_thread_id` when empty) to every `paseo_create_agent` call, also when the model calls it through Hermes' deferred `tool_call`.
- Local receiver for paseo-mcp webhooks (Standard Webhooks or paseo signature), `/healthz`.
- `agent.turn_ended` and `agent.permission_requested` become a turn of the session that started the agent (`ctx.inject_message(session_key=…)`), for messaging threads and gateway-local Desktop/TUI/CLI sessions.
- SQLite store: deduplication by delivery and event id, pending events survive restarts, compaction aliases.
- Coalescing per session, per-session rate limit, fallback `log` / `forward` / `session` with stored reasons.
- `/paseo-bridge` status command.

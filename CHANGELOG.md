# Changelog

## 0.1.0 (unreleased)

First release. Tested against Hermes v0.23.3 (`e2e/run-s5.sh`).

- Long-poll intake: `paseo_mcp_url` + a read token; `paseo_wait_for_events` with a cursor saved in the store, so events that happen while Hermes is down arrive after a restart.
- Steering: a `paseo_send_message` call routes that agent's next events to the calling session.
- Defaults: `agent.closed` and the `limit` outcome start a turn; `agent.turn_started` never does.
- Fallback `home` (default): a summary into the profile's home channel, else the log.
- `HERMES_PASEO_BRIDGE_<KEY>` environment variables override settings; `listen_host` must be loopback.
- The session key is read with Hermes' public `gateway.session_context.get_session_env`.
- `scripts/release-tarball.sh` (reproducible tarball + sha256), `pyproject.toml` (uv, pytest, ruff), `e2e/run-s5.sh`.

- `pre_tool_call` hook adds `hermes_session_key`, `hermes_profile`, `hermes_session_id`, `hermes_platform` (plus `origin_ref`, `hermes_chat_id`, `hermes_thread_id` when empty) to every `paseo_create_agent` call, also when the model calls it through Hermes' deferred `tool_call`.
- Local receiver for paseo-mcp webhooks (Standard Webhooks or paseo signature), `/healthz`.
- `agent.turn_ended` and `agent.permission_requested` become a turn of the session that started the agent (`ctx.inject_message(session_key=…)`), for messaging threads and gateway-local Desktop/TUI/CLI sessions.
- SQLite store: deduplication by delivery and event id, pending events survive restarts, compaction aliases.
- Coalescing per session, per-session rate limit, fallback `log` / `forward` / `session` with stored reasons.
- `/paseo-bridge` status command.

#!/usr/bin/env bash
# Runs inside the throwaway container as user agentbox (see run-s5.sh). No LLM, no accounts:
# a scripted OpenAI-compatible mock answers the turns, a fake stdio MCP server plays paseo-mcp's write
# tools, a fake paseo-mcp HTTP endpoint serves paseo_wait_for_events, and s5_driver.py talks to the
# gateway exactly like hermes-ui (gateway.sock ticket + WebSocket JSON-RPC).
set -euo pipefail

S5=/home/agentbox/s5
export HERMES_HOME=/home/agentbox/hermes-home
export MOCK_LLM_LOG=$S5/mock_llm.jsonl FAKE_PASEO_LOG=$S5/fake_paseo.jsonl
HERMES_TAG=${HERMES_TAG:-v0.23.3}
SRC=/home/agentbox/hermes-agent
fail=0
pass() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; fail=1; }

echo "== install Hermes $HERMES_TAG from upstream"
git clone -q --depth 1 --branch "$HERMES_TAG" https://github.com/NousResearch/hermes-agent "$SRC" 2>/dev/null
mkdir -p "$HERMES_HOME" /home/agentbox/work/demo
(cd "$SRC" && ./setup-hermes.sh --runtime-only > $S5/setup.log 2>&1) || { tail -20 $S5/setup.log; exit 1; }
hermes() { (cd "$SRC" && scripts/run-in-hermes-env ./hermes "$@" 2>/dev/null); }
PY=$(ls -d "$HERMES_HOME"/installs/*/environments/*/venv/bin/python | head -1)
hermes --version | head -1

echo "== drop the plugin in and enable it"
mkdir -p "$HERMES_HOME/plugins"
(cd $S5 && sha256sum -c hermes-paseo-bridge-*.tar.gz.sha256)
tar -xzf $S5/hermes-paseo-bridge-*.tar.gz -C "$HERMES_HOME/plugins"
hermes plugins enable hermes-paseo-bridge > /dev/null
hermes plugins list --plain --no-bundled | grep -q "enabled.*hermes-paseo-bridge" && pass "plugins list: enabled" || bad "plugins list"
hermes plugins doctor "$HERMES_HOME/plugins/hermes-paseo-bridge" --ci | grep -q "registrations: 0 tool(s), 2 hook(s)" \
  && pass "plugins doctor: loads, 2 hooks" || bad "plugins doctor"

echo "== configure (mock model, fake paseo MCP, bridge settings + secrets in .env)"
TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
{
  echo "HERMES_PASEO_BRIDGE_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  echo "HERMES_PASEO_BRIDGE_PASEO_MCP_URL=http://127.0.0.1:6768/mcp"
  echo "HERMES_PASEO_BRIDGE_PASEO_MCP_TOKEN=$TOKEN"
  echo "OPENAI_API_KEY=sk-mock"
} > "$HERMES_HOME/.env"
chmod 600 "$HERMES_HOME/.env"
cat > "$HERMES_HOME/config.yaml" <<EOF
model: {provider: custom, default: mock-model, base_url: "http://127.0.0.1:9999/v1", api_mode: chat_completions}
mcp_servers:
  paseo: {command: "$PY", args: ["$S5/fake_paseo_stdio.py"], env: {FAKE_PASEO_LOG: "$FAKE_PASEO_LOG"}}
plugins:
  enabled: [hermes-paseo-bridge]
  disabled: []
  entries:
    hermes-paseo-bridge:
      allow_gateway_injection: true
      settings: {listen_port: 8645, coalesce_seconds: 2}
EOF
nohup "$PY" $S5/mock_llm.py 9999 > $S5/mock_llm.out 2>&1 &
FAKE_PASEO_MCP_TOKEN=$TOKEN nohup "$PY" $S5/fake_paseo_mcp.py 6768 --sse > $S5/fake_http.out 2>&1 &
sleep 1

ss -ltn | grep -q ":8645 " && bad "receiver bound before the gateway runs" || pass "no receiver without a gateway"

gw_start() {
  (cd "$SRC" && nohup scripts/run-in-hermes-env ./hermes gateway run > "$S5/gateway-$(date +%s%N).out" 2>&1 &)
  for _ in $(seq 1 90); do ss -ltn | grep -q ":8645 " && return 0; sleep 1; done
  return 1
}
gw_stop() {
  local pid; pid=$(pgrep -f "^python3 .*/hermes gateway run" | head -1 || true)
  [ -n "$pid" ] && kill -TERM "$pid"
  for _ in $(seq 1 60); do pgrep -f "^python3 .*/hermes gateway run" > /dev/null || return 0; sleep 0.5; done
  return 1
}
drv() { (cd $S5 && "$PY" s5_driver.py "$@"); }

echo "== gateway"
gw_start && pass "gateway up, receiver listening" || { bad "receiver did not come up"; exit 1; }
ss -ltn | grep ":8645 " | grep -q "127.0.0.1:8645" && ! ss -ltn | grep ":8645 " | grep -qv "127.0.0.1:8645" \
  && pass "receiver binds 127.0.0.1 only: $(ss -ltn | grep ':8645 ' | awk '{print $4}')" || bad "receiver bind address"

echo "== (a) session key inside the tool-call path"
turn=$(drv turn "Please start a paseo agent for the failing login test" --keep 2)
SID=$(echo "$turn" | jq -r .session_id)
labels=$(grep '"paseo_create_agent"' "$FAKE_PASEO_LOG" | tail -1 | jq -c '.arguments.labels + {origin: "hermes"}')
key=$(echo "$labels" | jq -r .hermes_session_key)
[ "$key" = "agent:main:local:dm:$SID" ] && pass "label hermes_session_key=$key" || bad "session key label: $labels"
[ "$(echo "$labels" | jq -r .todo)" = "T-1" ] && pass "model's own labels kept" || bad "model labels lost"

echo "== (b) webhook -> inject into the UI session (client attached)"
r=$(drv live-inject --session "$SID" --agent agent-s5-0001 --labels "$labels" --seq 1 --wait 25)
[ "$(echo "$r" | jq .injected)" = true ] && pass "injected after $(echo "$r" | jq .latency_s)s; WS client saw: $(echo "$r" | jq -c '.assistant[0]')" || bad "live inject: $r"

echo "== (c) no client attached"
t0=$(date +%s.%N); drv post --agent agent-s5-0001 --labels "$labels" --seq 2 > /dev/null
[ "$(drv wait-update --seq 2 --since "$t0" --wait 20 | jq .injected)" = true ] && pass "injected without a client" || bad "inject without client"

echo "== (d) steering routes an outside agent's events to the steering session"
drv turn "Please steer agent agent-ui-0042 to also update docs" --session "$SID" --keep 1 > /dev/null
t0=$(date +%s.%N); drv post --agent agent-ui-0042 --labels '{"origin":"paseo-app"}' --seq 3 > /dev/null
[ "$(drv wait-update --seq 3 --since "$t0" --wait 20 | jq .injected)" = true ] && pass "steered agent reported back" || bad "steer route"

echo "== (e) gateway restart inside the coalescing window (webhook intake)"
t0=$(date +%s.%N); drv post --agent agent-s5-0001 --labels "$labels" --seq 4 > /dev/null
gw_stop && pass "gateway stopped with the event pending" || bad "gateway stop"
gw_start > /dev/null
[ "$(drv wait-update --seq 4 --since "$t0" --wait 25 | jq .injected)" = true ] && pass "pending event injected after restart" || bad "restart (webhook)"

echo "== (f) long-poll intake, event while Hermes is down"
gw_stop > /dev/null
t0=$(date +%s.%N)
curl -sf -X POST 127.0.0.1:6768/_emit -d "{\"type\":\"agent.permission_requested\",\"agentId\":\"agent-s5-0001\",\"title\":\"s5\",\"labels\":$labels,\"permission\":{\"requestId\":\"perm-1\",\"title\":\"Run tests (S5 marker 5)\"},\"summary\":\"perm\"}" > /dev/null
sleep 3
gw_start > /dev/null
[ "$(drv wait-update --seq 5 --since "$t0" --wait 30 | jq .injected)" = true ] && pass "event emitted while down arrived via the saved cursor" || bad "restart (poll)"

echo "== (g) signatures, duplicates, turn_started"
"$PY" - <<PYEOF || fail=1
import json, sys, urllib.request
sys.path.insert(0, "$HERMES_HOME/plugins/hermes-paseo-bridge")
from paseo_bridge import signature
secret = [l.split("=", 1)[1].strip() for l in open("$HERMES_HOME/.env") if l.startswith("HERMES_PASEO_BRIDGE_SECRET=")][0]
def post(body, did, key, sign=True):
    h = {"Content-Type": "application/json", **(signature.signing_headers("standard-webhooks", key, did, body) if sign else {})}
    r = urllib.request.Request("http://127.0.0.1:8645/paseo/events", data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
ev = {"id": "g:1", "type": "agent.turn_started", "agentId": "a", "labels": json.loads('$labels')}
body = json.dumps({"delivery_id": "g-1", "events": [ev], "event": ev, "type": ev["type"]}).encode()
checks = [("unsigned -> 401", post(body, "g-1", secret, sign=False)[0] == 401),
          ("wrong secret -> 401", post(body, "g-1", "whsec_" + "A" * 32)[0] == 401),
          ("turn_started stored but skipped", post(body, "g-1", secret) == (202, {"ok": True, "duplicate": False, "queued": 0, "skipped": 1, "already_seen": 0})),
          ("retry of the same delivery -> 200 duplicate", post(body, "g-1", secret)[0] == 200)]
for name, ok in checks:
    print(("PASS  " if ok else "FAIL  ") + name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
PYEOF

gw_stop > /dev/null || true
echo "== evidence"
grep -h "paseo-bridge\|Plugin message injection admitted" "$HERMES_HOME"/logs/agent.log "$HERMES_HOME"/logs/gateway.log | sort -u | cut -c1-220 | tail -12
[ $fail = 0 ] && echo "S5: ALL PASSED" || echo "S5: FAILURES"
exit $fail

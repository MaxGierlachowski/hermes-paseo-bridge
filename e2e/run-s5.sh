#!/usr/bin/env bash
# S5 end to end: Hermes (upstream tag, default v0.23.3) + this plugin in a throwaway Ubuntu 24.04
# container, without an LLM or any account. Builds the release tarball, installs it the way agentbox
# does (drop-in dir + `hermes plugins enable`), runs `hermes gateway run` and checks: session key in
# the tool-call path, injection into a gateway-local (hermes-ui style) session, steering, restarts
# (webhook and long-poll), signatures. The container is removed at the end.
#
#   e2e/run-s5.sh                     # native arch
#   PLATFORM=linux/amd64 e2e/run-s5.sh
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
name="${NAME:-ab-br-s5}"
platform="${PLATFORM:-}"
tag="${HERMES_TAG:-v0.23.3}"

"$root/scripts/release-tarball.sh" > /dev/null
docker rm -f "$name" > /dev/null 2>&1 || true
docker run -d --name "$name" ${platform:+--platform "$platform"} ubuntu:24.04 sleep infinity > /dev/null
trap 'docker rm -f "$name" > /dev/null 2>&1 || true' EXIT

docker exec "$name" bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  git curl ca-certificates xz-utils python3 libatomic1 iproute2 procps jq > /dev/null && useradd -m -u 10001 agentbox'
docker exec -u agentbox "$name" mkdir -p /home/agentbox/s5
for f in "$root"/dist/hermes-paseo-bridge-*.tar.gz* "$root"/e2e/*.py "$root"/e2e/s5-inside.sh "$root"/tests/fake_paseo_mcp.py; do
  docker cp "$f" "$name:/home/agentbox/s5/"
done
docker exec "$name" chown -R agentbox:agentbox /home/agentbox/s5
docker exec -u agentbox -e HERMES_TAG="$tag" "$name" bash /home/agentbox/s5/s5-inside.sh

#!/usr/bin/env bash
# End-to-end test of the production stack (docker-compose.prod.yml) with a locally built
# image: TLS through Caddy, two replicas, migrations, backups, admin, chat and streams.
# Uses the fake provider (tests/e2e/compose.e2e.yml), so it needs no keys and spends nothing.
#
#   scripts/e2e_prod.sh            # builds the image, runs the checks, tears everything down
#
# Needs ports 80, 443 and 127.0.0.1:8081 free. CI runs it on every pull request (ADR 0026).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
work="$(mktemp -d)"
project="gwe2e$$"
env_file="$work/env"
hex() { openssl rand -hex "$1"; }
cat > "$env_file" <<ENV
GATEWAY_VERSION=e2e
GATEWAY_DOMAIN=localhost
GATEWAY_ADMIN_KEY=$(hex 32)
REDIS_PASSWORD=$(hex 24)
POSTGRES_PASSWORD=$(hex 24)
GRAFANA_ADMIN_PASSWORD=$(hex 24)
GRAFANA_PG_PASSWORD=$(hex 24)
ENV
dc() { docker compose -p "$project" -f docker-compose.prod.yml -f tests/e2e/compose.e2e.yml --env-file "$env_file" "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- e2e FAILED; recent logs:"; dc logs --tail 40 gateway caddy migrate backup 2>&1 | tail -120 || true
  fi
  dc down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
admin_key="$(grep ^GATEWAY_ADMIN_KEY "$env_file" | cut -d= -f2)"
ok() { echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }

echo "build"
docker build -q -t ghcr.io/andrewttofi/llm-gateway:e2e . > /dev/null
echo "start"
dc up -d caddy gateway migrate redis redis-cache postgres backup > /dev/null 2>&1
for _ in $(seq 1 90); do
  curl -s http://127.0.0.1:8081/readyz | grep -q '"ready"' && break; sleep 1
done
curl -s http://127.0.0.1:8081/readyz | grep -q '"ready"' || fail "gateway never became ready"
ok "migrations ran, replicas ready"
healthy() { dc ps gateway --format '{{.Status}}' | grep -c '(healthy)' || true; }
for _ in $(seq 1 60); do [ "$(healthy)" = 2 ] && break; sleep 1; done  # Docker checks every 10 s
[ "$(healthy)" = 2 ] || fail "expected 2 healthy replicas, got $(healthy)"
ok "2 healthy replicas"

pub="https://localhost"
c() { curl -sk --max-time 30 "$@"; }
[ "$(c -o /dev/null -w '%{http_code}' $pub/healthz)" = 200 ] || fail "public /healthz"
c -I $pub/healthz | grep -qi '^strict-transport-security' || fail "HSTS header"
ok "TLS front door, HSTS"
for p in /admin/keys /metrics /readyz /docs /redoc /openapi.json; do
  [ "$(c -o /dev/null -w '%{http_code}' $pub$p)" = 404 ] || fail "public $p should be 404"
done
ok "operator endpoints hidden from the public site"

key="$(curl -s -X POST http://127.0.0.1:8081/admin/keys -H "Authorization: Bearer $admin_key" \
  -H 'content-type: application/json' -d '{"name":"e2e","tier":"chaos"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')"
ok "admin: key created"
auth=(-H "Authorization: Bearer $key" -H 'content-type: application/json')
chat='{"model":"chaos","messages":[{"role":"user","content":"hi"}]}'
r="$(c "${auth[@]}" -d "$chat" -w '\n%{http_code}' $pub/v1/chat/completions)"
[ "$(tail -1 <<<"$r")" = 200 ] || fail "chat: $(head -c 300 <<<"$r")"
ok "chat completion through TLS and both hops"
stream='{"model":"chaos","stream":true,"messages":[{"role":"user","content":"hi"}]}'
c -N "${auth[@]}" -d "$stream" $pub/v1/chat/completions | grep -q '^data: \[DONE\]' || fail "stream"
ok "stream completes"
msg='{"model":"chaos","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'
c -H "x-api-key: $key" -H 'content-type: application/json' -d "$msg" $pub/v1/messages \
  | grep -q '"type": *"message"' || fail "/v1/messages"
ok "Anthropic Messages API"
[ "$(c -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer gw_nope' -d "$chat" $pub/v1/chat/completions)" = 401 ] \
  || fail "bad key should be 401"
ok "bad key refused"

audit="$(curl -s http://127.0.0.1:8081/admin/audit -H "Authorization: Bearer $admin_key")"
grep -q '"key.create"' <<<"$audit" || fail "audit log"
ok "admin audit log"
for _ in $(seq 1 30); do dc exec -T backup sh -c 'ls /backups/*.dump' > /dev/null 2>&1 && break; sleep 1; done
dc exec -T backup sh /drill.sh > /dev/null || fail "backup restore drill"
ok "backup taken, restore drill passed"
sleep 3  # the usage writer flushes in batches
rows="$(dc exec -T postgres psql -U gateway -tAc 'SELECT count(*) FROM usage_log')"
[ "$rows" -ge 4 ] || fail "usage rows: $rows"
ok "usage log written ($rows rows)"
echo "e2e: all checks passed"

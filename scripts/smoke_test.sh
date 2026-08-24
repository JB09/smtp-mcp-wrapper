#!/usr/bin/env bash
#
# Smoke-test a built image by driving REAL MCP traffic against it.
#
#   scripts/smoke_test.sh <image-ref>
#
# `docker build` succeeding proves almost nothing about this server: the two
# ways an MCP SDK upgrade breaks it — binding the wrong interface, and the
# DNS-rebinding guard rejecting the proxy's `Host` — both produce an image that
# builds, starts, and reports **healthy** while every tool call fails. `/healthz`
# is not behind the guard, so it stays 200 throughout. Only a real
# `initialize` + `tools/list` over a non-localhost `Host` catches them.
#
# Phase 1 — the default (guard off) posture: the server is reachable from
#           outside its own loopback and advertises the expected tools.
# Phase 2 — the production posture: with MCP_ALLOWED_HOSTS set, the allowlisted
#           Host is accepted AND a foreign one is rejected with 421.

set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image-ref>}"

CONTAINER="${SMOKE_CONTAINER:-email-mcp-smoke}"
PORT="${SMOKE_PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"
# The Host header the container is expected to see in deployment: proxies
# generally rewrite it to the upstream address rather than the public route.
ROUTE_HOST="${SMOKE_ROUTE_HOST:-email-mcp:8080}"
FOREIGN_HOST="not-allowed.example:8080"
EXPECTED_TOOLS=("send_email")

failures=0

log()  { printf '\n=== %s\n' "$*"; }
pass() { printf '  ok   %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; failures=$((failures + 1)); }

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Wait for a line to appear in the container log. Returns 1 if it never does.
#
# The app writes these lines before uvicorn binds, so by the time /healthz
# answers they have certainly been *written* — but the daemon's log pipeline
# lags by a few milliseconds, and a single `docker logs | grep` loses that race
# on a loaded runner. It did on 2026-08-24: the guard-enabled grep missed a line
# that the failure dump printed 12ms later, in a run whose other checks (foreign
# Host -> 421) proved the guard was working. Polling keeps the assertion honest
# — a genuinely missing line still fails, just after the timeout instead of
# instantly.
wait_for_log() {
  local pattern="$1" deadline=$((SECONDS + ${2:-10}))
  while :; do
    if docker logs "$CONTAINER" 2>&1 | grep -q -- "$pattern"; then return 0; fi
    if [ "$SECONDS" -ge "$deadline" ]; then return 1; fi
    sleep 0.2
  done
}

start_container() {
  cleanup
  # Credentials are dummies on purpose — no phase sends mail, and STARTUP_TEST_EMAIL
  # stays off so the container never touches an SMTP server.
  docker run -d --name "$CONTAINER" \
    -p "127.0.0.1:${PORT}:8080" \
    -e SMTP_USER=smoke@example.com \
    -e SMTP_PASS=not-a-real-password \
    -e DEFAULT_TO=smoke@example.com \
    -e STARTUP_TEST_EMAIL=false \
    "$@" \
    "$IMAGE" >/dev/null

  for _ in $(seq 1 30); do
    if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done

  echo "container never became healthy; logs:" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
}

# POST an MCP request with an explicit Host header. Writes the body to $BODY and
# the response headers to $HEADERS; echoes the status code.
BODY=$(mktemp)
HEADERS=$(mktemp)
mcp_post() {
  local host="$1" payload="$2" session="${3:-}"
  local args=(-s -o "$BODY" -D "$HEADERS" -w '%{http_code}'
    -X POST "${BASE}/mcp"
    -H "Host: ${host}"
    -H 'Content-Type: application/json'
    -H 'Accept: application/json, text/event-stream')
  [ -n "$session" ] && args+=(-H "mcp-session-id: ${session}")
  # Never let a transport-level curl failure (server died, connection reset)
  # abort the run via `set -e` — report it as a failed check instead. curl
  # prints 000 when it never got a status line.
  curl "${args[@]}" -d "$payload" || printf 'curl-error-%s' "$?"
}

INIT_PAYLOAD='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke-test","version":"0"}}}'

session_id() { tr -d '\r' < "$HEADERS" | awk 'tolower($1) == "mcp-session-id:" { print $2 }'; }

# ---------------------------------------------------------------------------
log "Phase 1: handshake + tools/list over a non-localhost Host (guard off)"
start_container

# Cheapest possible check for the wrong bind interface (127.0.0.1:8000).
if wait_for_log 'Uvicorn running on http://0.0.0.0:8080'; then
  pass "listening on 0.0.0.0:8080"
else
  fail "not listening on 0.0.0.0:8080 — check host/port are passed to the serve call"
  docker logs "$CONTAINER" 2>&1 | tail -20
fi

code=$(mcp_post "$ROUTE_HOST" "$INIT_PAYLOAD")
if [ "$code" = "200" ]; then
  pass "initialize -> 200 (Host: ${ROUTE_HOST})"
else
  fail "initialize -> ${code} (Host: ${ROUTE_HOST})"
  cat "$BODY"
fi

SESSION=$(session_id)
if [ -z "$SESSION" ]; then
  fail "no mcp-session-id returned — cannot continue"
else
  mcp_post "$ROUTE_HOST" '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$SESSION" >/dev/null
  code=$(mcp_post "$ROUTE_HOST" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$SESSION")
  if [ "$code" = "200" ]; then
    pass "tools/list -> 200"
  else
    fail "tools/list -> ${code}"
    cat "$BODY"
  fi

  for tool in "${EXPECTED_TOOLS[@]}"; do
    if grep -q "\"name\":\"${tool}\"" "$BODY"; then
      pass "tool advertised: ${tool}"
    else
      fail "tool missing from tools/list: ${tool}"
      cat "$BODY"
    fi
  done
fi

# ---------------------------------------------------------------------------
log "Phase 2: DNS-rebinding guard, both directions (MCP_ALLOWED_HOSTS set)"
start_container -e "MCP_ALLOWED_HOSTS=${ROUTE_HOST}"

if wait_for_log "DNS-rebinding guard enabled"; then
  pass "guard reported enabled at startup"
else
  fail "guard not enabled — MCP_ALLOWED_HOSTS did not reach the app"
  docker logs "$CONTAINER" 2>&1 | tail -20
fi

code=$(mcp_post "$ROUTE_HOST" "$INIT_PAYLOAD")
if [ "$code" = "200" ]; then
  pass "allowlisted Host accepted -> 200"
else
  fail "allowlisted Host rejected -> ${code} (expected 200)"
  cat "$BODY"
fi

code=$(mcp_post "$FOREIGN_HOST" "$INIT_PAYLOAD")
if [ "$code" = "421" ]; then
  pass "foreign Host rejected -> 421"
else
  fail "foreign Host -> ${code} (expected 421 — the guard is not actually guarding)"
  cat "$BODY"
fi

# ---------------------------------------------------------------------------
# Everything above drives the *legacy* protocol: an `initialize` handshake and
# the Mcp-Session-Id it returns. The SDK picks the era per request from the
# MCP-Protocol-Version header, so a 2026-07-28 client takes a different code
# path entirely — one self-contained POST, no handshake, no session. Without
# this, an SDK bump could break every modern client while CI stays green.
# Reuses phase 2's container, so it also proves the modern path works with the
# Host guard on.
log "Phase 3: modern stateless request path (MCP-Protocol-Version: 2026-07-28)"

MODERN_VERSION="2026-07-28"
# With no handshake, what `initialize` used to establish rides on every request
# in a params._meta envelope instead. All three keys are required — omit them
# and the server answers 400 (-32602).
MODERN_META='"_meta":{'
MODERN_META+='"io.modelcontextprotocol/protocolVersion":"'"$MODERN_VERSION"'",'
MODERN_META+='"io.modelcontextprotocol/clientCapabilities":{},'
MODERN_META+='"io.modelcontextprotocol/clientInfo":{"name":"smoke-test","version":"0"}}'

# mcp_post takes a session, not arbitrary headers; the modern path needs the
# routing headers instead, so it gets its own poster.
mcp_post_modern() {
  local host="$1" method="$2" payload="$3"
  curl -s -o "$BODY" -D "$HEADERS" -w '%{http_code}' -X POST "${BASE}/mcp" \
    -H "Host: ${host}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "MCP-Protocol-Version: ${MODERN_VERSION}" \
    -H "Mcp-Method: ${method}" \
    -d "$payload" || printf 'curl-error-%s' "$?"
}

code=$(mcp_post_modern "$ROUTE_HOST" "tools/list" \
  "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{${MODERN_META}}}")
if [ "$code" = "200" ]; then
  pass "sessionless tools/list -> 200"
else
  fail "sessionless tools/list -> ${code} (expected 200)"
  cat "$BODY"
fi

# The point of the modern path is that there is no protocol session to store; a
# session id coming back means the request fell through to the legacy handler.
if [ -n "$(session_id)" ]; then
  fail "modern request returned an mcp-session-id — it was served by the legacy path"
else
  pass "no mcp-session-id returned"
fi

# server.py declares a cache hint for tools/list; a client that never sees it
# silently re-fetches the catalog on every reconnect.
if grep -q '"ttlMs"' "$BODY" && grep -q '"cacheScope":"public"' "$BODY"; then
  pass "tools/list carries the cache hint (ttlMs + cacheScope)"
else
  fail "tools/list result is missing ttlMs/cacheScope"
  cat "$BODY"
fi

# 2026-07-28 requires Mcp-Method (and Mcp-Name) to mirror the body so gateways
# can route on headers alone; the SDK rejects a mismatch with -32020. That
# guarantee is what makes per-tool proxy policy safe to write.
code=$(mcp_post_modern "$ROUTE_HOST" "tools/list" \
  "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"send_email\",\"arguments\":{},${MODERN_META}}}")
if [ "$code" = "400" ] && grep -q '\-32020' "$BODY"; then
  pass "Mcp-Method disagreeing with the body -> 400 (-32020)"
else
  fail "header/body mismatch -> ${code} (expected 400 with -32020)"
  cat "$BODY"
fi

# ---------------------------------------------------------------------------
# REQUIRE_POMERIUM_IDENTITY=true serves through a second, separate code path
# (`streamable_http_app()` + uvicorn) that needs its own bind address and
# security settings. Missing either breaks only this posture, so exercise it.
log "Phase 4: identity-gate serve path (REQUIRE_POMERIUM_IDENTITY=true)"
start_container \
  -e REQUIRE_POMERIUM_IDENTITY=true \
  -e POMERIUM_JWKS_URL=https://jwks.invalid/jwks.json \
  -e "MCP_ALLOWED_HOSTS=${ROUTE_HOST}"

if wait_for_log 'Uvicorn running on http://0.0.0.0:8080'; then
  pass "listening on 0.0.0.0:8080"
else
  fail "not listening on 0.0.0.0:8080 — check host is passed to the app builder"
  docker logs "$CONTAINER" 2>&1 | tail -20
fi

# No valid assertion is obtainable here (that needs a live Pomerium), so assert
# the gate rejects rather than that it admits: 401 proves the request reached
# the app's middleware, which is what a wrong bind would prevent.
code=$(mcp_post "$ROUTE_HOST" "$INIT_PAYLOAD")
if [ "$code" = "401" ]; then
  pass "unauthenticated /mcp rejected -> 401"
else
  fail "unauthenticated /mcp -> ${code} (expected 401)"
  cat "$BODY"
fi

# ---------------------------------------------------------------------------
if [ "$failures" -gt 0 ]; then
  printf '\nsmoke test FAILED (%d check(s))\n' "$failures"
  exit 1
fi
printf '\nsmoke test passed\n'

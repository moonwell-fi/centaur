#!/usr/bin/env bash
set -euo pipefail

# ── End-to-end Firewall integration test ────────────────────────────────────
# Tests: secret loading, credential injection, host blocking, passthrough.
# Uses a mock secrets service + DNS override to route api.anthropic.com
# to a local echo server.

NETWORK="fw-test-net"
MOCK_SECRETS="fw-test-secrets"
MOCK_UPSTREAM="fw-test-upstream"
PROXY_CONTAINER="fw-test-proxy"
CERTS_VOL="fw-test-certs"
TEST_ANTHROPIC_KEY="sk-test-anthropic-key-12345"
TEST_OPENAI_KEY="sk-test-openai-key-67890"
TEST_GITHUB_TOKEN="ghp_test_github_token_abcdef"

PASS=0
FAIL=0

pass() { echo "  ✅ $1"; PASS=$((PASS+1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

cleanup() {
    echo ""
    echo "═══ Cleaning up ═══"
    docker rm -f "$MOCK_SECRETS" "$MOCK_UPSTREAM" "$PROXY_CONTAINER" 2>/dev/null || true
    docker volume rm "$CERTS_VOL" 2>/dev/null || true
    docker network rm "$NETWORK" 2>/dev/null || true
}
trap cleanup EXIT

echo "═══ 1. Setup ═══"
docker network create "$NETWORK" 2>/dev/null || true
docker volume create "$CERTS_VOL" 2>/dev/null || true

echo ""
echo "═══ 2. Mock secrets service ═══"
docker run -d --name "$MOCK_SECRETS" --network "$NETWORK" \
    python:3.11-slim \
    python3 -c "
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

SECRETS = {
    'ANTHROPIC_API_KEY': '${TEST_ANTHROPIC_KEY}',
    'OPENAI_API_KEY': '${TEST_OPENAI_KEY}',
    'GITHUB_TOKEN': '${TEST_GITHUB_TOKEN}',
    'AMP_API_KEY': 'amp-test-key-xyz',
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'cached_keys': len(SECRETS)}).encode())
        elif self.path.startswith('/secrets/'):
            key = self.path.split('/secrets/', 1)[1]
            if key in SECRETS:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'value': SECRETS[key]}).encode())
            else:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'detail': 'not found'}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *a): pass

print('Mock secrets :8100', flush=True)
HTTPServer(('0.0.0.0', 8100), Handler).serve_forever()
" >/dev/null 2>&1

for i in $(seq 1 15); do
    docker exec "$MOCK_SECRETS" python3 -c "
import urllib.request; urllib.request.urlopen('http://localhost:8100/health', timeout=2)
" 2>/dev/null && break
    sleep 1
done
echo "  Mock secrets ready."

echo ""
echo "═══ 3. Mock upstream echo server (returns received headers as JSON) ═══"
docker run -d --name "$MOCK_UPSTREAM" --network "$NETWORK" \
    python:3.11-slim \
    python3 -c "
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class Handler(BaseHTTPRequestHandler):
    def _respond(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        body = json.dumps({'host': self.headers.get('Host',''), 'path': self.path, 'headers': headers}, indent=2)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body.encode())
    def do_GET(self): self._respond()
    def do_POST(self): self._respond()
    def log_message(self, *a): pass

print('Echo server :80', flush=True)
HTTPServer(('0.0.0.0', 80), Handler).serve_forever()
" >/dev/null 2>&1

for i in $(seq 1 10); do
    docker exec "$MOCK_UPSTREAM" python3 -c "
import urllib.request; urllib.request.urlopen('http://localhost:80/', timeout=2)
" 2>/dev/null && break
    sleep 1
done
echo "  Mock upstream ready."

echo ""
echo "═══ 4. Build & start firewall ═══"
docker build -t fw-test:latest services/firewall/ -q

# Get mock upstream IP for DNS overrides on the proxy
UPSTREAM_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$MOCK_UPSTREAM")

docker run -d --name "$PROXY_CONTAINER" --network "$NETWORK" \
    -e "SECRET_MANAGER_URL=http://${MOCK_SECRETS}:8100" \
    -e "FIREWALL_CACHE_TTL=2" \
    -v "${CERTS_VOL}:/certs" \
    --add-host "api.anthropic.com:${UPSTREAM_IP}" \
    --add-host "api.openai.com:${UPSTREAM_IP}" \
    --add-host "api.github.com:${UPSTREAM_IP}" \
    --add-host "api.ampcode.com:${UPSTREAM_IP}" \
    fw-test:latest

for i in $(seq 1 30); do
    HEALTH=$(docker exec "$PROXY_CONTAINER" curl -sf http://localhost:8081/health 2>/dev/null || true)
    if [ -n "$HEALTH" ]; then break; fi
    sleep 1
done
echo "  Proxy ready: $HEALTH"

# Wait for cert + secret refresh
sleep 3

echo ""
echo "═══ 5. Running tests ═══"
echo ""

# Helper: run curl through the proxy (proxy handles DNS → mock upstream)
run_proxy_curl() {
    local url="$1"
    shift
    docker run --rm --network "$NETWORK" \
        curlimages/curl:latest \
        curl -sf --proxy "http://${PROXY_CONTAINER}:8080" \
        "$url" --max-time 10 "$@" 2>/dev/null || echo "{}"
}

echo "── Test 1: Anthropic API key injection ──"
RESULT=$(run_proxy_curl "http://api.anthropic.com/v1/messages")
INJECTED=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('headers',{}).get('x-api-key','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")
if [ "$INJECTED" = "$TEST_ANTHROPIC_KEY" ]; then
    pass "Anthropic x-api-key injected correctly"
else
    fail "Anthropic x-api-key: expected '$TEST_ANTHROPIC_KEY', got '$INJECTED'"
fi

echo ""
echo "── Test 2: OpenAI Bearer token injection ──"
RESULT=$(run_proxy_curl "http://api.openai.com/v1/chat/completions")
INJECTED=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('headers',{}).get('authorization','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")
EXPECTED="Bearer $TEST_OPENAI_KEY"
if [ "$INJECTED" = "$EXPECTED" ]; then
    pass "OpenAI authorization Bearer token injected correctly"
else
    fail "OpenAI authorization: expected '$EXPECTED', got '$INJECTED'"
fi

echo ""
echo "── Test 3: GitHub token injection ──"
RESULT=$(run_proxy_curl "http://api.github.com/user")
INJECTED=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('headers',{}).get('authorization','MISSING'))" 2>/dev/null || echo "PARSE_ERROR")
EXPECTED="token $TEST_GITHUB_TOKEN"
if [ "$INJECTED" = "$EXPECTED" ]; then
    pass "GitHub token injected correctly"
else
    fail "GitHub authorization: expected '$EXPECTED', got '$INJECTED'"
fi

echo ""
echo "── Test 4: Blocked host (secrets service) ──"
BLOCKED=$(docker run --rm --network "$NETWORK" \
    curlimages/curl:latest \
    curl -s -o /dev/null -w "%{http_code}" --proxy "http://${PROXY_CONTAINER}:8080" \
    "http://secrets:8100/health" --max-time 5 2>/dev/null || echo "000")
if [ "$BLOCKED" = "403" ]; then
    pass "Access to 'secrets' host blocked (HTTP 403)"
else
    fail "Blocked host returned HTTP $BLOCKED, expected 403"
fi

echo ""
echo "── Test 5: Non-matched host passthrough (no injection) ──"
RESULT=$(run_proxy_curl "http://${MOCK_UPSTREAM}:80/test-passthrough")
HAS_XAPI=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'x-api-key' in d.get('headers',{}) else 'no')" 2>/dev/null || echo "PARSE_ERROR")
HAS_AUTH=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if 'authorization' in d.get('headers',{}) else 'no')" 2>/dev/null || echo "PARSE_ERROR")
if [ "$HAS_XAPI" = "no" ] && [ "$HAS_AUTH" = "no" ]; then
    pass "Non-matched host: no credentials injected"
else
    fail "Non-matched host had unexpected headers injected"
fi

echo ""
echo "── Test 6: Health endpoint reports secrets loaded ──"
HEALTH=$(docker exec "$PROXY_CONTAINER" curl -sf http://localhost:8081/health 2>/dev/null)
LOADED=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin)['secrets_loaded'])" 2>/dev/null || echo "0")
if [ "$LOADED" -ge 4 ]; then
    pass "Health reports $LOADED secrets loaded"
else
    fail "Health reports only $LOADED secrets (expected ≥4)"
fi

echo ""
echo "── Test 7: CA cert shared to volume ──"
if docker run --rm -v "${CERTS_VOL}:/certs" alpine test -f /certs/ca-cert.pem 2>/dev/null; then
    pass "CA cert shared at /certs/ca-cert.pem"
else
    fail "CA cert not found in shared volume"
fi

echo ""
echo "═══ Results: $PASS passed, $FAIL failed ═══"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1

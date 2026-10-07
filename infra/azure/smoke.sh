#!/usr/bin/env bash
# Post-deploy smoke checks (t-iac-smoke-script). Runnable by the deploy workflow and by hand.
#
# Usage: infra/azure/smoke.sh <base-url> [--xff-burst]
#   <base-url>   https://<app>.<region>.azurecontainerapps.io (no trailing path needed)
#   --xff-burst  also run the per-IP signin-bucket burst (check 5); see the warning there
#
# Automates the scriptable parts of docs/deploy-cutover-runbook.md section 7 (HTTPS, http:// not
# serving the app, /api/health, / serves the SPA) and section 7a "After deploy: TRUSTED_PROXY_HOPS
# and Host passthrough" (same-origin write is not refused by the CSRF Host/Origin check; one-network
# half of the X-Forwarded-For burst). Reads no secrets, signs nobody in, and never uses a real
# username: sign-in attempts use a random non-existent username and print only the error `code`.
#
# Exit status: 0 all checks passed, 1 at least one failed, 2 usage error.

set -euo pipefail

usage() {
  echo "Usage: $0 <base-url> [--xff-burst]" >&2
  echo "  e.g. $0 https://<app>.<region>.azurecontainerapps.io" >&2
  exit 2
}

[[ $# -ge 1 ]] || usage
BASE=""
XFF_BURST=0
for arg in "$@"; do
  case "$arg" in
    --xff-burst) XFF_BURST=1 ;;
    -h | --help) usage ;;
    http://* | https://*) [[ -z "$BASE" ]] || usage; BASE="$arg" ;;
    *) usage ;;
  esac
done
[[ -n "$BASE" ]] || usage

# Origin = scheme://host[:port], exactly as a browser serialises it (backend/app/core/csrf.py
# compares it with Host). Anything after the authority is dropped.
SCHEME="${BASE%%://*}"
AUTHORITY="${BASE#*://}"
AUTHORITY="${AUTHORITY%%/*}"
ORIGIN="${SCHEME}://${AUTHORITY}"
HOSTNAME_ONLY="${AUTHORITY%%:*}"

# Signin bucket from backend/app/core/ratelimit.py: SIGNIN = Bucket("signin", 10, 15 * MINUTE, IP).
# Keep in sync if that row (and docs/api-contract.md "Rate limits and lockout") changes.
SIGNIN_CAPACITY=10
SIGNIN_SPENT=0 # signin tokens this run has spent so far (check 4 spends one)

CURL=(curl -sS --max-time 15)
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=1; }
skip() { printf 'SKIP  %s\n' "$*"; }

rand_hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# Print only the error envelope's `code` ({"error": {"code": ...}}), never the rest of the body.
error_code() {
  grep -o '"code"[[:space:]]*:[[:space:]]*"[A-Z_]*"' "$1" 2>/dev/null | head -n1 | sed 's/.*"\([A-Z_]*\)"$/\1/' || true
}

# One same-origin sign-in attempt with a random unknown username and random password.
# Optional $1: X-Forwarded-For value to spoof. Prints the HTTP status; body in $TMP/signin.json.
signin_attempt() {
  local body xff=()
  body="{\"username\":\"smoke-$(rand_hex 8)\",\"password\":\"$(rand_hex 16)\"}"
  [[ $# -ge 1 ]] && xff=(-H "X-Forwarded-For: $1")
  "${CURL[@]}" -o "$TMP/signin.json" -w '%{http_code}' \
    -X POST "$ORIGIN/api/v2/auth/signin" \
    -H 'Content-Type: application/json' \
    -H "Origin: $ORIGIN" \
    -H 'Sec-Fetch-Site: same-origin' \
    "${xff[@]}" \
    --data "$body" || true
}

echo "Smoke checks against $ORIGIN"

# 1. Health.
code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$ORIGIN/api/health" || true)"
if [[ "$code" == 200 ]]; then
  pass "1 GET /api/health -> 200"
else
  fail "1 GET /api/health -> $code (expected 200)"
fi

# 2. The SPA shell. `<div id="root">` is the mount point in frontend/index.html and survives the
#    Vite build unchanged.
meta="$("${CURL[@]}" -o "$TMP/index.html" -w '%{http_code} %{content_type}' "$ORIGIN/" || true)"
code="${meta%% *}"
ctype="${meta#* }"
if [[ "$code" != 200 ]]; then
  fail "2 GET / -> $code (expected 200)"
elif [[ "${ctype,,}" != text/html* ]]; then
  fail "2 GET / -> content-type '$ctype' (expected text/html)"
elif ! grep -q 'id="root"' "$TMP/index.html"; then
  fail "2 GET / -> HTML without the SPA root marker id=\"root\""
else
  pass "2 GET / -> 200 text/html with the SPA root"
fi

# 3. Plain http:// must not serve the app: a redirect to https:// or a refused connection is fine.
if [[ "$SCHEME" == http ]]; then
  if [[ "$HOSTNAME_ONLY" == 127.0.0.1 || "$HOSTNAME_ONLY" == localhost ]]; then
    skip "3 http:// check (local loopback run)"
  else
    fail "3 base URL is http://; a deployment must be checked over https://"
  fi
else
  if meta="$("${CURL[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' "http://$AUTHORITY/" 2>/dev/null)"; then
    code="${meta%% *}"
    location="${meta#* }"
    case "$code" in
      301 | 302 | 307 | 308)
        if [[ "$location" == https://* ]]; then
          pass "3 http:// -> $code redirect to https"
        else
          fail "3 http:// -> $code redirect to '$location' (expected an https:// target)"
        fi
        ;;
      200) fail "3 http:// -> 200: the app is served over plain HTTP (allowInsecure must be false)" ;;
      *) fail "3 http:// -> $code (expected a redirect to https or a refused connection)" ;;
    esac
  else
    pass "3 http:// connection refused/failed (not served)"
  fi
fi

# 4. Host passthrough. A same-origin write with bad credentials must reach the handler and get 401.
#    403 means the CSRF check saw Origin != Host, i.e. the ingress rewrote Host.
code="$(signin_attempt)"
SIGNIN_SPENT=$((SIGNIN_SPENT + 1))
ecode="$(error_code "$TMP/signin.json")"
case "$code" in
  401) pass "4 same-origin POST signin (unknown user) -> 401 ${ecode:-}" ;;
  403) fail "4 same-origin POST signin -> 403 ${ecode:-}: Host/Origin mismatch behind the ingress (Host rewritten?)" ;;
  429) fail "4 same-origin POST signin -> 429 ${ecode:-}: signin bucket for this IP already drained; retry in ~15 min" ;;
  *) fail "4 same-origin POST signin -> $code ${ecode:-} (expected 401)" ;;
esac

# 5. --xff-burst: trip the per-IP signin bucket exactly once.
#
#    The bucket holds SIGNIN_CAPACITY tokens per 15 minutes per client IP (refill 1 per 90 s, a
#    refused request spends nothing). Check 4 already spent one, so the first 429 is expected on
#    burst request (SIGNIN_CAPACITY - SIGNIN_SPENT + 1); we send exactly that many and expect all
#    but the last to be 401. (The runbook's "121 -> one 429" is the 120/min public-read bucket; this
#    uses the signin bucket so the burst is 10 requests, not 121.)
#
#    Each request spoofs a different X-Forwarded-For. If the app keyed buckets on a spoofed hop
#    (TRUSTED_PROXY_HOPS too high for the ingress), every request would get a fresh bucket and no
#    429 would come: FAIL.
#
#    Limits: this uses the runner's single IP, so it only proves the one-network half of
#    t-am-verify-aca-xff. The second-network half (a spoofed request from another network inside
#    the window still gets through, i.e. the bucket is not shared by everyone behind the ingress)
#    stays manual per the runbook. WARNING: the burst drains the signin bucket for this IP for
#    ~15 minutes; real sign-ins, recovery and password changes from this IP get 429 until it refills.
if [[ "$XFF_BURST" == 1 ]]; then
  n=$((SIGNIN_CAPACITY - SIGNIN_SPENT + 1))
  first_429=0
  count_429=0
  other=""
  for ((i = 1; i <= n; i++)); do
    code="$(signin_attempt "203.0.113.$(((RANDOM % 254) + 1))")"
    case "$code" in
      429)
        count_429=$((count_429 + 1))
        [[ "$first_429" -ne 0 ]] || first_429=$i
        ;;
      401) ;;
      *) other+=" #$i:$code" ;;
    esac
  done
  if [[ -n "$other" ]]; then
    fail "5 xff burst: unexpected statuses${other} (expected 401 then 429)"
  elif [[ "$count_429" -eq 1 && "$first_429" -eq "$n" ]]; then
    pass "5 xff burst: $((n - 1)) x 401 then 1 x 429 at request $n (bucket keyed on the real client IP)"
  elif [[ "$count_429" -eq 0 ]]; then
    fail "5 xff burst: no 429 in $n requests; buckets follow the spoofed X-Forwarded-For (check TRUSTED_PROXY_HOPS)"
  else
    fail "5 xff burst: first 429 at request $first_429 of $n ($count_429 total); bucket was partly drained (earlier run within 15 min?) or the capacity changed"
  fi
fi

if [[ "$FAILED" -ne 0 ]]; then
  echo "Smoke checks FAILED"
  exit 1
fi
echo "All smoke checks passed"

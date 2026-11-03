#!/usr/bin/env bash
# Smoke test against a running Arranger API.
#
#   scripts/smoke.sh http://127.0.0.1:8000
#
# Checks, in order: /health and /ready answer 200; /auth/me answers 401 with no
# session; a throwaway account can register, sign in with a cookie jar, list
# its profiles, and sign out with the CSRF header; and, when SMOKE_CAPABILITIES
# is set, /catalog reports those capabilities as available. One "ok" or "FAIL"
# line per check; the exit status is non-zero if any check failed.
#
# Needs only bash and curl. Every assertion is on an HTTP status code, so there
# is no dependency on jq or python.
#
# Environment:
#   SMOKE_ORIGIN               Origin header on unsafe requests. Defaults to BASE_URL.
#                              The API refuses cookie-carrying writes without an
#                              allowed Origin, exactly as it would for a browser.
#   SMOKE_CAPABILITIES         Comma-separated /catalog capabilities that must be
#                              available, e.g. "export_pdf,import_audio". Unset
#                              skips the check (a minimal image lacks LilyPond).
#   SMOKE_TIMEOUT_SECONDS      Per-request timeout. Default 20.
set -euo pipefail

usage() {
  echo "usage: $0 BASE_URL" >&2
  exit 2
}

BASE_URL="${1:-}"
[[ -n "$BASE_URL" ]] || usage
BASE_URL="${BASE_URL%/}"
ORIGIN="${SMOKE_ORIGIN:-$BASE_URL}"
TIMEOUT="${SMOKE_TIMEOUT_SECONDS:-20}"

JAR="$(mktemp)"
BODY="$(mktemp)"
trap 'rm -f "$JAR" "$BODY"' EXIT

failures=0

pass() { printf 'ok    %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1"
  failures=$((failures + 1))
}

# request METHOD PATH [curl options...]
# Prints the HTTP status ("000" when the server could not be reached); the
# response body is left in $BODY. Cookies go through the jar both ways.
request() {
  local method="$1" path="$2" code
  shift 2
  # curl writes "000" itself when it never got a response; a failed exit
  # status must not add a second one.
  code="$(curl --silent --max-time "$TIMEOUT" \
    --output "$BODY" --write-out '%{http_code}' \
    --request "$method" --cookie "$JAR" --cookie-jar "$JAR" \
    "$@" "$BASE_URL$path" 2> /dev/null)" || true
  printf '%s' "${code:-000}"
}

# check DESCRIPTION EXPECTED_STATUS ACTUAL_STATUS
check() {
  local what="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$what -> $actual"
  else
    fail "$what -> expected $expected, got $actual: $(head -c 300 "$BODY" | tr '\n' ' ')"
  fi
}

# The CSRF token is the value of the arranger_csrf cookie (double-submit).
# Netscape jar format: domain, flag, path, secure, expiry, name, value.
csrf_token() {
  awk '$6 == "arranger_csrf" { print $7 }' "$JAR" | tail -n 1
}

json_header=(-H "Content-Type: application/json")
origin_header=(-H "Origin: $ORIGIN")

# --- unauthenticated ------------------------------------------------------------

check "GET /health" 200 "$(request GET /health)"
check "GET /ready" 200 "$(request GET /ready)"
check "GET /auth/me without a session" 401 "$(request GET /auth/me)"

# --- register, sign in, use the session, sign out --------------------------------

EMAIL="smoke-$(date +%s)-$RANDOM@example.com"
PASSWORD="smoke-passphrase-$RANDOM-$RANDOM"
credentials="{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}"
# Registration takes a display name; login refuses any field it did not ask for.
registration="{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"display_name\":\"Smoke\"}"

check "POST /auth/register" 200 \
  "$(request POST /auth/register "${json_header[@]}" "${origin_header[@]}" --data "$registration")"

# Registering signs the account in. Drop that session so the login is a real one.
: > "$JAR"
check "GET /auth/me after discarding the cookies" 401 "$(request GET /auth/me)"

check "POST /auth/login" 200 \
  "$(request POST /auth/login "${json_header[@]}" "${origin_header[@]}" --data "$credentials")"
if [[ -z "$(csrf_token)" ]]; then
  fail "login set no arranger_csrf cookie; the CSRF header cannot be sent"
fi
check "GET /auth/me with the session cookie" 200 "$(request GET /auth/me)"
check "GET /profiles with the session cookie" 200 "$(request GET /profiles)"

check "POST /auth/logout with the CSRF header" 200 \
  "$(request POST /auth/logout "${origin_header[@]}" -H "X-CSRF-Token: $(csrf_token)")"
check "GET /auth/me after logout" 401 "$(request GET /auth/me)"

# --- capabilities the deployment promises --------------------------------------------

if [[ -n "${SMOKE_CAPABILITIES:-}" ]]; then
  check "GET /catalog" 200 "$(request GET /catalog)"
  IFS=',' read -r -a capabilities <<< "$SMOKE_CAPABILITIES"
  for capability in "${capabilities[@]}"; do
    capability="${capability// /}"
    [[ -n "$capability" ]] || continue
    # The capability is an object such as {"available": true, ...}; read it without jq.
    if grep -o "\"$capability\": *{[^}]*}" "$BODY" | grep -q '"available": *true'; then
      pass "capability $capability is available"
    else
      fail "capability $capability is not available: $(grep -o "\"$capability\": *{[^}]*}" "$BODY" | head -c 300)"
    fi
  done
fi

# --- verdict -------------------------------------------------------------------------------

if (( failures > 0 )); then
  echo "$failures check(s) failed against $BASE_URL"
  exit 1
fi
echo "all checks passed against $BASE_URL"

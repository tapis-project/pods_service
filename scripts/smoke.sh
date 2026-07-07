#!/usr/bin/env bash
# smoke.sh — one-command deploy triage for the pods service.
#
# Decodes WHICH LAYER is broken from response shapes, so a UI full of
# "CORS request did not succeed" doesn't send you searching the wrong stack:
#
#   timeout / conn refused        -> site nginx or network
#   502 Bad Gateway (nginx)       -> pods-api down (crashloop: alembic? DB?)
#   text/plain "404 page not found" -> traefik has NO dynamic config
#                                      -> restart pods-health-central
#   JSON error envelope           -> service healthy; problem is client-side
#
# Usage:
#   scripts/smoke.sh                          # tacc.develop, unauthenticated
#   scripts/smoke.sh https://tacc.tapis.io    # another base URL
#   TAPIS_JWT=<token> scripts/smoke.sh        # adds authed checks (fleet
#                                             # metrics summary + admin health)
set -u

BASE="${1:-https://tacc.develop.tapis.io}"
JWT="${TAPIS_JWT:-}"

pass=0; fail=0
say()  { printf '%s\n' "$*"; }
ok()   { say "  ✅ $*"; pass=$((pass+1)); }
bad()  { say "  ❌ $*"; fail=$((fail+1)); }

BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT

probe() {  # probe <url> -> sets STATUS, CTYPE, BODY (300-char preview; full body in $BODY_FILE)
    local url="$1" out
    out=$(curl -sk -m 10 -o "$BODY_FILE" -w '%{http_code} %{content_type}' \
        ${JWT:+-H "X-Tapis-Token: $JWT"} "$url" 2>/dev/null)
    STATUS="${out%% *}"
    CTYPE="${out#* }"
    BODY=$(head -c 300 "$BODY_FILE" 2>/dev/null)
}

say "── pods service smoke: $BASE ${JWT:+(authed)} ──"

# 1. API reachability through the whole chain
probe "$BASE/v3/pods"
case "$STATUS" in
    000) bad "/v3/pods: no response — site nginx or network is down" ;;
    502) bad "/v3/pods: 502 from nginx — pods-api is DOWN (check: kubectl logs deploy/pods-api — alembic? DB conn?)" ;;
    404) if [[ "$CTYPE" == application/json* ]]; then
             bad "/v3/pods: JSON 404 — api answered but route missing (very wrong image?)"
         else
             bad "/v3/pods: plain-text 404 — traefik has NO routes. Fix: kubectl rollout restart deploy/pods-health-central (regenerates traefik configmap), then bounce traefik if needed"
         fi ;;
    *)   if [[ "$CTYPE" == application/json* ]]; then
             ok "/v3/pods: HTTP $STATUS JSON — nginx→traefik→pods-api chain is healthy"
         else
             bad "/v3/pods: HTTP $STATUS non-JSON — unexpected: ${BODY}"
         fi ;;
esac

# 2. Fleet metrics endpoint present? (distinguishes old image from new)
probe "$BASE/v3/pods/metrics"
if [[ "$CTYPE" == application/json* ]]; then
    if [[ "$STATUS" == 404 ]]; then
        bad "/v3/pods/metrics: JSON 404 — api is up but running an image WITHOUT the fleet metrics routes"
    else
        ok "/v3/pods/metrics: HTTP $STATUS JSON — fleet metrics routes deployed"
    fi
elif [[ "$STATUS" != 000 && "$STATUS" != 502 && "$STATUS" != 404 ]]; then
    bad "/v3/pods/metrics: HTTP $STATUS $CTYPE — unexpected: ${BODY}"
else
    say "  … /v3/pods/metrics skipped conclusions — blocked by the layer failure above"
fi

# 3. Authed checks (only with TAPIS_JWT)
if [[ -n "$JWT" ]]; then
    probe "$BASE/v3/pods/metrics"
    if [[ "$STATUS" == 200 ]]; then
        summary=$(python3 -c '
import json,sys
try:
    r = json.load(open(sys.argv[1]))["result"]
    pods = r.get("pods", {})
    live = sum(1 for p in pods.values() if p.get("usage"))
    print(f"{len(pods)} pods, {live} with live usage, backend={r.get(\"backend\",{}).get(\"name\")}"
          + (f", UNAVAILABLE: {r[\"unavailable\"]}" if "unavailable" in r else ""))
except Exception as e:
    print(f"parse failed: {e}")' "$BODY_FILE" 2>/dev/null) || summary="(summary unavailable)"
        ok "fleet metrics: $summary"
    else
        bad "authed /v3/pods/metrics: HTTP $STATUS — ${BODY}"
    fi

    probe "$BASE/v3/pods/admin/health"
    if [[ "$STATUS" == 200 ]]; then
        mb=$(python3 -c '
import json,sys
try:
    b = json.load(open(sys.argv[1]))["result"].get("metrics_backend", {})
    print(f"backend={b.get(\"backend\")} configured={b.get(\"configured\")} reachable={b.get(\"reachable\")}"
          + (f" GUIDANCE: {b[\"guidance\"]}" if b.get("guidance") else ""))
except Exception:
    print("(no metrics_backend block — old image?)")' "$BODY_FILE" 2>/dev/null)
        ok "admin health metrics_backend: $mb"
    else
        say "  … /v3/pods/admin/health: HTTP $STATUS (needs admin role; not a failure)"
    fi
fi

say "── $pass ok, $fail failing ──"
exit $((fail > 0))

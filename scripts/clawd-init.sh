#!/bin/bash
# clawd runtime init — runs at pod start (everything heavy is already baked into the image).
# Configures OpenClaw's LiteLLM backend + wires OpenClaw's MCP client at the Tapis Pods
# *bridge* (audited MCP at $PODS_MCP_URL), with a Tapis token kept fresh by client creds.
#
# ── ASSUMPTIONS (flagged; parameterized so they're easy to correct) ──────────────────────
#  1. $PODS_MCP_URL is a LIVE, internet-reachable bridge endpoint (…/mcp). Currently the bridge
#     runs locally only; this assumes it's hosted (e.g. deployed as its own Pods stack).
#  2. OpenClaw reads MCP servers from ~/.openclaw/openclaw.json under an "mcp.servers" map with
#     {type:http,url,headers}. VERIFY this key against OpenClaw's own docs — adjust write_mcp().
#  3. The hosted bridge honors a forwarded X-Tapis-Token header per request (README TODO:
#     get_http_headers forwarding). Assumed live per the deploy plan.
set -u
OC=~/.openclaw/openclaw.json
mkdir -p ~/.openclaw /config/data/User

# ── OpenClaw LiteLLM backend (unchanged from the inline tag) ──
openclaw onboard --non-interactive --auth-choice litellm-api-key \
  --litellm-api-key "${LITELLM_API_KEY:-}" --custom-base-url "${LITELLM_BASE_URL:-}" 2>/dev/null || true
python3 - <<'PY' || true
import json, os
cfg = {"models": {"mode": "merge", "providers": {"litellm": {
    "baseUrl": os.environ.get("LITELLM_BASE_URL", ""),
    "apiKey": os.environ.get("LITELLM_API_KEY", ""),
    "api": "openai-completions", "models": []}}},
    "agents": {"defaults": {"models": {"litellm/*": {}}}}}
p = os.path.expanduser("~/.openclaw/openclaw.json")
try:
    with open(p) as f: cur = json.load(f)
except Exception: cur = {}
cur.update(cfg)
open(p, "w").write(json.dumps(cur, indent=2))
PY
printf 'LITELLM_API_KEY=%s\nOPENAI_API_KEY=%s\nOPENAI_BASE_URL=%s\n' \
  "${LITELLM_API_KEY:-}" "${LITELLM_API_KEY:-}" "${LITELLM_BASE_URL:-}" > ~/.openclaw/.env && chmod 600 ~/.openclaw/.env

# ── Tapis token from client creds (auto-refresh) ──
mint_token() {
  [ -n "${TAPIS_CLIENT_BASIC:-}" ] && [ "${TAPIS_CLIENT_BASIC:-none}" != "none" ] || return 1
  curl -fsS -X POST "${TAPIS_BASE_URL%/}/v3/oauth2/tokens" \
    -H "Authorization: Basic ${TAPIS_CLIENT_BASIC}" -H "Content-Type: application/json" \
    -d "{\"username\":\"${TAPIS_USERNAME:-}\",\"password\":\"${TAPIS_PASSWORD:-}\",\"grant_type\":\"password\"}" \
    2>/dev/null | jq -r '.result.access_token.access_token // empty'
}

# ── write OpenClaw's MCP server block pointing at the hosted bridge, with the fresh token ──
write_mcp() {  # $1 = token
  TOKEN="$1" MCP_URL="${PODS_MCP_URL:-none}" python3 - <<'PY' || true
import json, os
url = os.environ.get("MCP_URL", "none"); tok = os.environ.get("TOKEN", "")
if url in ("", "none"): raise SystemExit(0)
p = os.path.expanduser("~/.openclaw/openclaw.json")
try:
    with open(p) as f: cur = json.load(f)
except Exception: cur = {}
cur.setdefault("mcp", {}).setdefault("servers", {})["tapis-pods"] = {
    "type": "http", "url": url, "headers": {"X-Tapis-Token": tok}}
open(p, "w").write(json.dumps(cur, indent=2))
PY
}

TOKEN="$(mint_token)" || TOKEN=""
[ -n "$TOKEN" ] && write_mcp "$TOKEN" && echo "→ Tapis MCP wired: ${PODS_MCP_URL}" \
  || echo "→ Tapis MCP NOT wired (set PODS_MCP_URL + TAPIS_CLIENT_BASIC/USERNAME/PASSWORD)"

# refresh well before the ~4h token expiry; rewrites config (OpenClaw reconnects the MCP on next use)
( while true; do sleep 3000; T="$(mint_token)" && [ -n "$T" ] && write_mcp "$T"; done ) &

# ── VS Code settings: terminal auto-launches openclaw, dark, copilot off ──
[ ! -f /config/data/User/settings.json ] && cat > /config/data/User/settings.json <<'JSON'
{ "workbench.colorTheme": "Default Dark+", "workbench.startupEditor": "terminal",
  "terminal.integrated.defaultProfile.linux": "openclaw",
  "terminal.integrated.profiles.linux": { "openclaw": { "path": "/bin/bash", "args": ["-c", "openclaw; exec bash -i"] } },
  "github.copilot.enable": { "*": false }, "extensions.autoCheckUpdates": false }
JSON

# ── git identity + optional repo clone ──
if [ -n "${GITHUB_TOKEN:-}" ] && [ "${GITHUB_TOKEN}" != "none" ]; then
  git config --global credential.helper store && echo "https://oauth2:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
fi
[ -n "${GIT_USER_NAME:-}" ] && [ "${GIT_USER_NAME}" != "none" ] && git config --global user.name "${GIT_USER_NAME}"
[ -n "${GIT_USER_EMAIL:-}" ] && [ "${GIT_USER_EMAIL}" != "none" ] && git config --global user.email "${GIT_USER_EMAIL}"
if [ -n "${GIT_REPO:-}" ] && [ "${GIT_REPO}" != "none" ]; then
  D=/config/workspace/$(basename "${GIT_REPO}" .git); [ ! -d "$D" ] && git clone "${GIT_REPO}" "$D" 2>/dev/null || true
fi
echo "→ clawd ready. Type 'openclaw' in the terminal (auto-opens)."

#!/bin/bash
# hermes runtime init — per-instance config only (installs are baked into the image).
set -u
export PATH="$HOME/.local/bin:$HOME/.hermes/bin:$PATH"
mkdir -p ~/.hermes /config/data/User

# discover the real model id from LiteLLM (falls back to AI_MODEL)
ACTUAL_MODEL=$(curl -sf "${LITELLM_BASE_URL%/}/models" -H "Authorization: Bearer ${LITELLM_API_KEY:-}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || echo "${AI_MODEL:-gpt-4o}")

# hermes config (custom LiteLLM provider; api_mode:chat_completions avoids the Ollama-probe hang #26489)
printf 'model:\n  default: %s\n  provider: custom\n  base_url: %s\n  api_key: %s\n  context_length: 128000\ncustom_providers:\n  - name: litellm\n    base_url: %s\n    key_env: LITELLM_API_KEY\n    api_mode: chat_completions\n' \
  "$ACTUAL_MODEL" "${LITELLM_BASE_URL:-}" "${LITELLM_API_KEY:-}" "${LITELLM_BASE_URL:-}" > ~/.hermes/config.yaml
printf 'LITELLM_API_KEY=%s\nOPENAI_API_KEY=%s\nOPENAI_BASE_URL=%s\n' \
  "${LITELLM_API_KEY:-}" "${LITELLM_API_KEY:-}" "${LITELLM_BASE_URL:-}" > ~/.hermes/.env && chmod 600 ~/.hermes/.env

# VS Code settings: terminal auto-launches hermes
[ ! -f /config/data/User/settings.json ] && cat > /config/data/User/settings.json <<'JSON'
{ "workbench.colorTheme": "Default Dark+", "workbench.startupEditor": "terminal",
  "terminal.integrated.defaultProfile.linux": "hermes",
  "terminal.integrated.profiles.linux": { "hermes": { "path": "/bin/bash", "args": ["-c", "hermes; exec bash -i"] } } }
JSON

# git identity + optional clone
if [ -n "${GITHUB_TOKEN:-}" ] && [ "${GITHUB_TOKEN}" != "none" ]; then
  git config --global credential.helper store && echo "https://oauth2:${GITHUB_TOKEN}@github.com" > /root/.git-credentials
fi
[ -n "${GIT_USER_NAME:-}" ] && [ "${GIT_USER_NAME}" != "none" ] && git config --global user.name "${GIT_USER_NAME}"
[ -n "${GIT_USER_EMAIL:-}" ] && [ "${GIT_USER_EMAIL}" != "none" ] && git config --global user.email "${GIT_USER_EMAIL}"
if [ -n "${GIT_REPO:-}" ] && [ "${GIT_REPO}" != "none" ]; then
  D=/config/workspace/$(basename "${GIT_REPO}" .git); [ ! -d "$D" ] && git clone "${GIT_REPO}" "$D" 2>/dev/null || true
fi

# hermes gateway in the background so the vscode extension can connect
hermes gateway run 2>/dev/null &
echo "→ hermes ready. Type 'hermes' in the terminal (auto-opens)."
